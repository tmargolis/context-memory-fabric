package capture

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
)

// What each app's checkpoint hook runs, and whether it's current (v0.1.5):
// a hook that runs an older cmf, or an older cmf-capture plugin, still
// nudges but misses newer behaviour (0.1.4's project hint), and nothing
// said so. The Features cards and Status rows use this.

// hookBinary is the program a `cmf hook stop` command runs ("" for others).
func hookBinary(command string) string {
	command = strings.TrimSpace(command)
	if strings.HasPrefix(command, `"`) {
		if q, err := strconv.QuotedPrefix(command); err == nil {
			p, _ := strconv.Unquote(q)
			return p
		}
	}
	first, _, _ := strings.Cut(command, " ")
	return first
}

// BinaryVersion runs `<path> version` ("cmf 0.1.4" → "0.1.4"); "" if it
// can't. Tests replace it.
var BinaryVersion = func(path string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, path, "version").Output()
	if err != nil {
		return ""
	}
	v, ok := strings.CutPrefix(strings.TrimSpace(string(out)), "cmf ")
	if !ok {
		return ""
	}
	return v
}

// claudePluginVersion is the cmf-capture version Claude Code has installed
// (~/.claude/plugins/installed_plugins.json), "" if none.
func claudePluginVersion(userHome string) string {
	data, err := os.ReadFile(filepath.Join(userHome, ".claude", "plugins", "installed_plugins.json"))
	if err != nil {
		return ""
	}
	var f struct {
		Plugins map[string][]struct {
			Version string `json:"version"`
		} `json:"plugins"`
	}
	if json.Unmarshal(data, &f) != nil {
		return ""
	}
	if v := f.Plugins[Plugin]; len(v) > 0 {
		return v[0].Version
	}
	return ""
}

// coworkHasPlugin: Cowork keeps its own plugin list per account
// (local-agent-mode-sessions/<account>/<org>/rpm/manifest.json); a plugin
// installed in Claude Code isn't in it.
func coworkHasPlugin(userHome string) bool {
	matches, _ := filepath.Glob(filepath.Join(userHome, "Library", "Application Support", "Claude", "local-agent-mode-sessions", "*", "*", "rpm", "manifest.json"))
	for _, m := range matches {
		data, err := os.ReadFile(m)
		if err != nil {
			continue
		}
		var f struct {
			Plugins []struct {
				Name string `json:"name"`
			} `json:"plugins"`
		}
		if json.Unmarshal(data, &f) != nil {
			continue
		}
		for _, p := range f.Plugins {
			if p.Name == "cmf-capture" {
				return true
			}
		}
	}
	return false
}

func baseVersion(v string) string {
	return strings.TrimSuffix(strings.TrimPrefix(strings.TrimSpace(v), "v"), "-dev")
}

// older reports whether dotted version a is older than b (0.1.3 < 0.1.10).
func older(a, b string) bool {
	pa, pb := strings.Split(baseVersion(a), "."), strings.Split(baseVersion(b), ".")
	for i := 0; i < len(pa) || i < len(pb); i++ {
		var x, y int
		if i < len(pa) {
			x, _ = strconv.Atoi(strings.SplitN(pa[i], "-", 2)[0])
		}
		if i < len(pb) {
			y, _ = strconv.Atoi(strings.SplitN(pb[i], "-", 2)[0])
		}
		if x != y {
			return x < y
		}
	}
	return false
}

// DescribeHook fills HookLine and HookProblem: what the source's checkpoint
// hook is, and what's wrong with it, against the server's version and the
// newest cmf-capture plugin (both may be "" when unknown).
func DescribeHook(st *SourceState, serverVersion, latestPlugin string) {
	st.HookLine, st.HookProblem, st.HookUpdatable = "", "", false
	switch st.Hook {
	case "cmf":
		where := st.HookBinary
		switch {
		case st.HookVersion == "":
			st.HookLine = "checkpoint hook: cmf hook stop (" + where + ")"
			st.HookProblem = "the hook runs " + where + ", which isn't there or doesn't answer; reinstall the hook (`cmf capture hook install " + st.Harness + "`)"
		default:
			st.HookLine = fmt.Sprintf("checkpoint hook: cmf hook stop (cmf %s at %s)", st.HookVersion, where)
			if serverVersion != "" && baseVersion(st.HookVersion) != baseVersion(serverVersion) {
				st.HookProblem = fmt.Sprintf("the hook runs cmf %s but the server is %s; update it so checkpoints carry the newest behaviour", st.HookVersion, serverVersion)
				st.HookUpdatable = true
			}
		}
	case "plugin":
		v := st.PluginVersion
		if v == "" {
			st.HookLine = "checkpoint hook: the cmf-capture plugin"
		} else {
			st.HookLine = "checkpoint hook: the cmf-capture plugin " + v
		}
		if v != "" && latestPlugin != "" && older(v, latestPlugin) {
			st.HookProblem = fmt.Sprintf("cmf-capture %s is installed and %s is out: in Claude Code run `/plugin marketplace update context-memory-fabric`, then update cmf-capture under `/plugin` → Installed", v, latestPlugin)
		}
	case "python":
		st.HookLine = "checkpoint hook: the older Python checkpoint hook"
		st.HookProblem = "replace it with the cmf hook (`cmf capture hook install " + st.Harness + "`)"
	default:
		if st.Present {
			st.HookLine = "checkpoint hook: none"
			if st.ID == "claude_cowork" {
				// Cowork runs its sessions in the cloud, so few transcripts reach
				// this computer for the poller; the plugin's hook runs in the
				// session itself. It's added in the account, not by cmf.
				st.HookProblem = "Cowork sessions run in the cloud, so the poller sees little of them; the cmf-capture plugin captures them from inside the session. In Claude Desktop, two steps: (1) Customize → Plugins → Add → Add marketplace, enter `tmargolis/context-memory-fabric`; (2) adding the marketplace doesn't install anything, so then find cmf-capture under Plugins → Discover and install it"
			}
		}
	}
}
