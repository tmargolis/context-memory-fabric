package capture

import (
	"os"
	"path/filepath"
	"strings"

	contextmemoryfabric "github.com/tmargolis/context-memory-fabric"
)

// Whether an app's own instructions file holds CMF's guidance (v0.1.7), for
// the apps whose instructions live in a file on this computer. Claude's
// account instructions (Chat, Cowork) can't be read here; Status shows
// whether those apps capture instead (/health live_captures).

// CoreTools are the tools the instruction block routes that every install
// has; the block's other tools (search_wiki, search_knowledge,
// propose_doc_update) are optional, since the doc says to drop lines for
// tools you don't use.
var CoreTools = []string{"get_context", "recall_mem", "remember", "capture_session"}

// InstructionsFile is one app's instructions file and what CMF found in it.
type InstructionsFile struct {
	App     string   `json:"app"`
	Path    string   `json:"path"`
	Exists  bool     `json:"exists"`
	State   string   `json:"state"`             // "current", "partial", "none"
	Missing []string `json:"missing,omitempty"` // core tools a "partial" file doesn't name
}

// instructionFiles maps an app (clients ID) to its user-level instructions file.
func instructionFiles(userHome string, getenv func(string) string) map[string]string {
	codexHome := filepath.Join(userHome, ".codex")
	if getenv != nil && getenv("CODEX_HOME") != "" {
		codexHome = getenv("CODEX_HOME")
	}
	return map[string]string{
		"claude-code": filepath.Join(userHome, ".claude", "CLAUDE.md"),
		"codex":       filepath.Join(codexHome, "AGENTS.md"),
	}
}

// CheckInstructions reads each app's instructions file.
func CheckInstructions(userHome string, getenv func(string) string) map[string]InstructionsFile {
	out := map[string]InstructionsFile{}
	for app, path := range instructionFiles(userHome, getenv) {
		f := InstructionsFile{App: app, Path: path, State: "none"}
		data, err := os.ReadFile(path)
		if err == nil {
			f.Exists = true
			f.State, f.Missing = classifyInstructions(string(data))
		}
		out[app] = f
	}
	return out
}

// classifyInstructions: "current" names CMF and every core tool; "partial"
// mentions CMF but lacks some (an older or trimmed block); "none" otherwise.
func classifyInstructions(text string) (string, []string) {
	lower := strings.ToLower(text)
	mentions := strings.Contains(lower, "context memory fabric") || strings.Contains(text, "CMF")
	var missing []string
	for _, tool := range CoreTools {
		if !strings.Contains(text, tool) {
			missing = append(missing, tool)
		}
	}
	switch {
	case len(missing) == 0 && mentions:
		return "current", nil
	case mentions || len(missing) < len(CoreTools):
		return "partial", missing
	}
	return "none", nil
}

// blockHasCoreTools guards CoreTools against the block changing.
func blockHasCoreTools() []string {
	var absent []string
	for _, tool := range CoreTools {
		if !strings.Contains(contextmemoryfabric.AgentInstructions(), tool) {
			absent = append(absent, tool)
		}
	}
	return absent
}
