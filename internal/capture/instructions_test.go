package capture

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	contextmemoryfabric "github.com/tmargolis/context-memory-fabric"
)

func TestBlockNamesTheCoreTools(t *testing.T) {
	if absent := blockHasCoreTools(); len(absent) > 0 {
		t.Fatalf("INSTRUCTIONS-FOR-AGENTS.md no longer names %v; update CoreTools", absent)
	}
}

func TestCheckInstructions(t *testing.T) {
	user := t.TempDir()
	got := CheckInstructions(user, nil)
	if got["codex"].Exists || got["codex"].State != "none" || got["claude-code"].Path != filepath.Join(user, ".claude", "CLAUDE.md") {
		t.Fatalf("%+v", got)
	}
	os.MkdirAll(filepath.Join(user, ".codex"), 0o700)
	os.WriteFile(filepath.Join(user, ".codex", "AGENTS.md"), []byte("# Mine\n\n"+contextmemoryfabric.AgentInstructions()), 0o600)
	os.MkdirAll(filepath.Join(user, ".claude"), 0o700)
	os.WriteFile(filepath.Join(user, ".claude", "CLAUDE.md"), []byte("Use CMF. Call get_context and recall_mem.\n"), 0o600)
	got = CheckInstructions(user, nil)
	if got["codex"].State != "current" {
		t.Fatalf("%+v", got["codex"])
	}
	if c := got["claude-code"]; c.State != "partial" || strings.Join(c.Missing, ",") != "remember,capture_session" {
		t.Fatalf("%+v", c)
	}
	alt := t.TempDir()
	if p := CheckInstructions(user, func(k string) string {
		if k == "CODEX_HOME" {
			return alt
		}
		return ""
	})["codex"].Path; p != filepath.Join(alt, "AGENTS.md") {
		t.Fatal(p)
	}
	if s, _ := classifyInstructions("Be concise. Use British spelling."); s != "none" {
		t.Fatal(s)
	}
}
