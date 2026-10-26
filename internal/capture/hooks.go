package capture

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/tmargolis/context-memory-fabric/internal/home"
	"github.com/tmargolis/context-memory-fabric/internal/jsonedit"
)

// Plugin is the cmf-capture Claude plugin (Claude Code, Code tab, Cowork).
const Plugin = "cmf-capture@context-memory-fabric"

// Legacy worker hooks start a host-side Python tail pass; the poller
// container does that job now. They're reported, never touched.
var workerMarkers = map[string]string{"claude_code": "cmf-claude-code-worker", "codex": "cmf-codex-worker", "antigravity": "cmf-antigravity-worker"}

// HookFile is where each harness keeps its Stop hooks.
func HookFile(harness, userHome string) string {
	switch harness {
	case "claude_code":
		return filepath.Join(userHome, ".claude", "settings.json")
	case "codex":
		return filepath.Join(userHome, ".codex", "hooks.json")
	case "antigravity":
		return filepath.Join(userHome, ".gemini", "config", "hooks.json")
	}
	return ""
}

// Hooks is what a harness's hook file holds that concerns CMF.
type Hooks struct {
	Harness string `json:"harness"`
	Path    string `json:"path"`
	Nudge   string `json:"nudge,omitempty"`  // "plugin", "cmf" (this binary), "python" or ""
	Binary  string `json:"binary,omitempty"` // the program a "cmf" nudge runs
	Worker  bool   `json:"worker,omitempty"`
	Err     string `json:"error,omitempty"`
}

// commands lists every hook command for event under a harness's file layout.
func commands(harness string, root *jsonedit.Object, event string) []string {
	var out []string
	collect := func(raw json.RawMessage) {
		var groups []map[string]any
		json.Unmarshal(raw, &groups)
		for _, g := range groups {
			if c, ok := g["command"].(string); ok { // antigravity: flat handlers
				out = append(out, c)
			}
			hs, _ := g["hooks"].([]any)
			for _, h := range hs {
				if m, ok := h.(map[string]any); ok {
					if c, ok := m["command"].(string); ok {
						out = append(out, c)
					}
				}
			}
		}
	}
	if harness == "antigravity" {
		for _, name := range root.Keys() {
			child, err := root.Child(name)
			if err != nil {
				continue
			}
			if raw, ok := child.Get(event); ok {
				collect(raw)
			}
		}
		return out
	}
	hooks, err := root.Child("hooks")
	if err != nil {
		return nil
	}
	if raw, ok := hooks.Get(event); ok {
		collect(raw)
	}
	return out
}

func ReadHooks(harness, userHome string) Hooks {
	h := Hooks{Harness: harness, Path: HookFile(harness, userHome)}
	data, err := os.ReadFile(h.Path)
	if err != nil {
		if !os.IsNotExist(err) {
			h.Err = err.Error()
		}
		return h
	}
	root, err := jsonedit.Parse(data)
	if err != nil {
		h.Err = fmt.Sprintf("can't read %s: %v", h.Path, err)
		return h
	}
	for _, c := range commands(harness, root, "Stop") {
		if strings.Contains(c, Marker) {
			if strings.Contains(c, " hook stop ") {
				h.Nudge, h.Binary = "cmf", hookBinary(c)
			} else {
				h.Nudge = "python"
			}
		}
		if strings.Contains(c, workerMarkers[harness]) {
			h.Worker = true
		}
	}
	if harness == "claude_code" && h.Nudge == "" {
		if raw, ok := root.Get("enabledPlugins"); ok {
			var plugins map[string]any
			if json.Unmarshal(raw, &plugins) == nil && plugins[Plugin] == true {
				h.Nudge = "plugin"
			}
		}
	}
	return h
}

// HookCommand is the Stop hook command for this binary.
func HookCommand(cmf, harness string) string {
	return fmt.Sprintf("%q hook stop --harness %s # %s", cmf, harness, Marker)
}

// HookChange is one planned hook-file rewrite.
type HookChange struct {
	Harness string
	Path    string
	Old     []byte
	New     []byte
	Note    string
}

// PlanHook installs (or with remove, takes out) this binary's nudge. An
// existing nudge of any kind (the plugin, the Python one) counts as
// installed, so there is never a second checkpoint; remove takes out only
// nudges, never the worker hooks or anything else.
func PlanHook(harness, userHome, cmf string, remove bool) (*HookChange, error) {
	h := ReadHooks(harness, userHome)
	if h.Err != "" {
		return nil, fmt.Errorf("%s", h.Err)
	}
	c := &HookChange{Harness: harness, Path: h.Path}
	if data, err := os.ReadFile(h.Path); err == nil {
		c.Old = data
	}
	switch {
	case !remove && (h.Nudge == "plugin" || h.Nudge == "cmf"):
		// The older Python hook (Nudge "python") is replaced below: Status
		// tells you to run this to do it, and it used to stop here.
		c.New, c.Note = c.Old, map[string]string{"plugin": "already captured by the cmf-capture plugin", "cmf": "already has the cmf checkpoint hook"}[h.Nudge]
		return c, nil
	case remove && (h.Nudge == "" || h.Nudge == "plugin"):
		c.New, c.Note = c.Old, "no checkpoint hook to remove"
		if h.Nudge == "plugin" {
			c.Note = "captured by the cmf-capture plugin; remove it in Claude's /plugin menu"
		}
		return c, nil
	}
	if cmf == "" && !remove {
		return nil, fmt.Errorf("cmf runs from a temporary place; move it somewhere lasting (e.g. /Applications or ~/bin) so the hook can keep calling it")
	}
	root, err := jsonedit.Parse(c.Old)
	if err != nil {
		return nil, err
	}
	handler := map[string]any{"type": "command", "command": HookCommand(cmf, harness), "timeout": 10}
	if harness == "antigravity" {
		if remove {
			root.Remove(Marker)
			c.Note = "remove the " + Marker + " entry"
		} else {
			entry := jsonedit.New()
			entry.SetValue("Stop", []any{handler})
			root.Set(Marker, entry.Raw()) // replaces a Python hook's entry in place
			c.Note = "add a " + Marker + " Stop entry running cmf"
			if h.Nudge == "python" {
				c.Note = "replace the Python checkpoint hook with one running cmf"
			}
		}
	} else {
		hooks, err := root.Child("hooks")
		if err != nil {
			return nil, err
		}
		var groups []json.RawMessage
		if raw, ok := hooks.Get("Stop"); ok {
			json.Unmarshal(raw, &groups)
		}
		var kept []json.RawMessage
		for _, g := range groups {
			// Removing drops the checkpoint hook; installing drops a Python
			// one it replaces (a cmf one never gets here).
			if strings.Contains(string(g), Marker) {
				continue
			}
			kept = append(kept, g)
		}
		if remove {
			c.Note = "remove the checkpoint hook"
		} else {
			group := map[string]any{"hooks": []any{handler}}
			if harness == "claude_code" {
				group = map[string]any{"matcher": "", "hooks": []any{handler}}
			}
			raw, _ := json.Marshal(group)
			kept = append(kept, raw)
			c.Note = "add a Stop hook running cmf"
			if h.Nudge == "python" {
				c.Note = "replace the Python checkpoint hook with one running cmf"
			}
		}
		if len(kept) == 0 {
			hooks.Remove("Stop")
		} else {
			hooks.SetValue("Stop", kept)
		}
		root.Set("hooks", hooks.Raw())
	}
	c.New, err = root.File()
	return c, err
}

func (c *HookChange) Empty() bool { return string(c.Old) == string(c.New) }

// Write backs the file up into b (refusing if it changed since planned) and writes it.
func (c *HookChange) Write(b *home.Backup) error {
	if c.Empty() {
		return nil
	}
	now, err := os.ReadFile(c.Path)
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	if string(now) != string(c.Old) {
		return fmt.Errorf("%s changed since cmf read it; run the command again", c.Path)
	}
	if err := b.Save(c.Path); err != nil {
		return err
	}
	perm := os.FileMode(0o644)
	if info, err := os.Stat(c.Path); err == nil {
		perm = info.Mode().Perm()
	}
	return home.WriteFile(c.Path, c.New, perm)
}
