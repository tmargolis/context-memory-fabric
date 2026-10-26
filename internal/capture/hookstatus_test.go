package capture

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestHookBinary(t *testing.T) {
	for cmd, want := range map[string]string{
		`"/Applications/cmf" hook stop --harness codex # cmf-capture-nudge`:        "/Applications/cmf",
		`"/Users/a b/bin/cmf" hook stop --harness antigravity # cmf-capture-nudge`: "/Users/a b/bin/cmf",
		`/usr/local/bin/cmf hook stop --harness codex # cmf-capture-nudge`:         "/usr/local/bin/cmf",
	} {
		if got := hookBinary(cmd); got != want {
			t.Errorf("%s: %q", cmd, got)
		}
	}
}

func TestOlder(t *testing.T) {
	for _, c := range []struct {
		a, b string
		want bool
	}{{"0.1.0", "0.1.2", true}, {"0.1.10", "0.1.9", false}, {"0.1.4-dev", "0.1.4", false}, {"v0.1.3", "0.1.4", true}, {"0.2.0", "0.1.9", false}} {
		if got := older(c.a, c.b); got != c.want {
			t.Errorf("older(%s, %s) = %v", c.a, c.b, got)
		}
	}
}

func TestDescribeHook(t *testing.T) {
	st := SourceState{Source: Source{ID: "codex", Harness: "codex"}, Present: true, Hook: "cmf", HookBinary: "/Applications/cmf", HookVersion: "0.1.3"}
	DescribeHook(&st, "0.1.4", "0.1.2")
	if st.HookLine != "checkpoint hook: cmf hook stop (cmf 0.1.3 at /Applications/cmf)" || !st.HookUpdatable ||
		!strings.Contains(st.HookProblem, "runs cmf 0.1.3 but the server is 0.1.4") {
		t.Fatalf("%+v", st)
	}
	st.HookVersion = "0.1.4-dev"
	if DescribeHook(&st, "0.1.4", ""); st.HookProblem != "" || st.HookUpdatable {
		t.Fatalf("a dev build of the same version matches: %+v", st)
	}
	st.HookVersion = ""
	if DescribeHook(&st, "0.1.4", ""); !strings.Contains(st.HookProblem, "isn't there or doesn't answer") || st.HookUpdatable {
		t.Fatalf("%+v", st)
	}

	p := SourceState{Source: Source{ID: "claude_code"}, Present: true, Hook: "plugin", PluginVersion: "0.1.0"}
	if DescribeHook(&p, "0.1.4", "0.1.2"); !strings.Contains(p.HookProblem, "cmf-capture 0.1.0 is installed and 0.1.2 is out") {
		t.Fatalf("%+v", p)
	}
	c := SourceState{Source: Source{ID: "claude_cowork"}, Present: true}
	if DescribeHook(&c, "0.1.4", "0.1.2"); !strings.Contains(c.HookProblem, "(2) adding the marketplace doesn't install anything") {
		t.Fatalf("%+v", c)
	}
}

func TestPluginLists(t *testing.T) {
	user := t.TempDir()
	if claudePluginVersion(user) != "" || coworkHasPlugin(user) {
		t.Fatal("nothing installed yet")
	}
	write := func(path, body string) {
		os.MkdirAll(filepath.Dir(path), 0o700)
		os.WriteFile(path, []byte(body), 0o600)
	}
	write(filepath.Join(user, ".claude", "plugins", "installed_plugins.json"),
		`{"version":2,"plugins":{"cmf-capture@context-memory-fabric":[{"scope":"user","version":"0.1.0"}]}}`)
	rpm := filepath.Join(user, "Library", "Application Support", "Claude", "local-agent-mode-sessions", "acct", "org", "rpm", "manifest.json")
	write(rpm, `{"plugins":[{"name":"career-navigator"}]}`)
	if claudePluginVersion(user) != "0.1.0" || coworkHasPlugin(user) {
		t.Fatal("Claude Code has it; Cowork doesn't")
	}
	write(rpm, `{"plugins":[{"name":"career-navigator"},{"name":"cmf-capture"}]}`)
	if !coworkHasPlugin(user) {
		t.Fatal("Cowork lists it now")
	}
}
