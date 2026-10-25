package clients

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/home"
)

var target = Target{Port: 8000, PublicURL: "https://cmf.example.ts.net"}

func setup(t *testing.T) (Env, *home.Backup) {
	dir := t.TempDir()
	env := Env{Home: dir, OS: "darwin"}
	b, err := home.Home{Dir: filepath.Join(dir, ".cmf")}.NewBackup(time.Date(2026, 10, 24, 12, 0, 0, 0, time.UTC))
	if err != nil {
		t.Fatal(err)
	}
	return env, b
}

func write(t *testing.T, path, body string) {
	os.MkdirAll(filepath.Dir(path), 0o700)
	if err := os.WriteFile(path, []byte(body), 0o644); err != nil {
		t.Fatal(err)
	}
}

func app(id string) App { a, _ := Find(id); return a }

const desktop = `{
  "mcpServers": {
    "filesystem": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]},
    "cmf-old": {"command": "uv", "args": ["run", "--directory", "/repo", "python", "-m", "server.mcp"]},
    "memory": {"command": "npx", "args": ["-y", "mcp-remote", "http://127.0.0.1:8001/mcp"]}
  },
  "globalShortcut": "Alt+Space"
}
`

func TestDesktopDuplicatesReplacedByOne(t *testing.T) {
	env, b := setup(t)
	a := app("claude-desktop")
	write(t, a.Path(env), desktop)
	s := Inspect(a, env, target)
	if !s.Installed || len(s.CMF()) != 1 || s.CMF()[0].Name != "cmf-old" || s.CMF()[0].Kind != "stdio" {
		t.Fatalf("%+v", s.Entries) // "memory" points at another port and isn't named CMF: left alone
	}
	c, err := Plan(s, target, true)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(c.Summary, "; ") != `remove "cmf-old" (starts its own CMF server); add "context-memory-fabric" → http://127.0.0.1:8000/mcp via npx mcp-remote` {
		t.Fatal(c.Summary)
	}
	if err := c.Write(b, target, env, true); err != nil {
		t.Fatal(err)
	}
	got, _ := os.ReadFile(a.Path(env))
	text := string(got)
	if !strings.Contains(text, `"args": [
        "-y",
        "mcp-remote",
        "http://127.0.0.1:8000/mcp"`) || strings.Contains(text, "server.mcp") {
		t.Fatal(text)
	}
	if strings.Index(text, `"filesystem"`) > strings.Index(text, `"memory"`) || strings.Index(text, `"mcpServers"`) > strings.Index(text, `"globalShortcut"`) {
		t.Fatalf("key order changed:\n%s", text)
	}
	if _, err := os.Stat(filepath.Join(b.Dir(), "external", strings.TrimPrefix(a.Path(env), "/"))); err != nil {
		t.Fatal("no backup", err)
	}
	if c2, _ := Plan(Inspect(a, env, target), target, true); !c2.Empty() {
		t.Fatal("second run should change nothing", c2.Summary)
	}
}

const codex = `model = "gpt-5.5"

[mcp_servers.node_repl]
command = "/Applications/ChatGPT.app/node_repl"
args = ["--x"]

[mcp_servers.node_repl.env]
CODEX_HOME = "/Users/t/.codex"

[mcp_servers.context_memory_fabric]
enabled = true
url = "https://cmf.example.ts.net/mcp"

[mcp_servers.context_memory_fabric.http_headers]
X = "y"

[projects."/Users/t/Dev/x"]
trust_level = "trusted"
`

func TestCodexPublicURLEntryIsKept(t *testing.T) {
	env, _ := setup(t)
	a := app("codex")
	write(t, a.Path(env), codex)
	s := Inspect(a, env, target)
	if !s.Healthy() {
		t.Fatalf("%+v", s.Entries)
	}
	c, _ := Plan(s, target, true)
	if !c.Empty() {
		t.Fatal(c.Summary)
	}
}

func TestCodexStaleEntryReplacedSubtablesToo(t *testing.T) {
	env, b := setup(t)
	a := app("codex")
	write(t, a.Path(env), codex)
	local := Target{Port: 8000} // no public URL: the Funnel entry doesn't reach this install
	s := Inspect(a, env, local)
	if len(s.CMF()) != 1 || s.CMF()[0].Correct {
		t.Fatalf("%+v", s.Entries)
	}
	c, err := Plan(s, local, true)
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Write(b, local, env, true); err != nil {
		t.Fatal(err)
	}
	got, _ := os.ReadFile(a.Path(env))
	text := string(got)
	for _, keep := range []string{"[mcp_servers.node_repl]", "[mcp_servers.node_repl.env]", `CODEX_HOME = "/Users/t/.codex"`, `[projects."/Users/t/Dev/x"]`, `trust_level = "trusted"`} {
		if !strings.Contains(text, keep) {
			t.Errorf("lost %q:\n%s", keep, text)
		}
	}
	if strings.Contains(text, "ts.net") || strings.Contains(text, "http_headers]") || strings.Count(text, "[mcp_servers.context_memory_fabric]") != 1 ||
		!strings.HasSuffix(text, "[mcp_servers.context_memory_fabric]\nurl = \"http://127.0.0.1:8000/mcp\"\n") {
		t.Fatal(text)
	}
}

func TestClaudeCodeKeepsEveryOtherKey(t *testing.T) {
	env, b := setup(t)
	a := app("claude-code")
	write(t, a.Path(env), `{"numStartups":5,"projects":{"/x":{"mcpServers":{}}},"zzz":[1,2]}`)
	tok := Target{Port: 8000, Token: "secret-token"}
	c, err := Plan(Inspect(a, env, tok), tok, true)
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Write(b, tok, env, true); err != nil {
		t.Fatal(err)
	}
	got, _ := os.ReadFile(a.Path(env))
	text := string(got)
	if !(strings.Index(text, "numStartups") < strings.Index(text, "projects") && strings.Index(text, "projects") < strings.Index(text, "zzz") &&
		strings.Index(text, "zzz") < strings.Index(text, "mcpServers\": {\n    \"context")) {
		t.Fatal(text)
	}
	if !strings.Contains(text, `"Authorization": "Bearer secret-token"`) || !strings.Contains(text, `"type": "http"`) {
		t.Fatal(text)
	}
	if info, _ := os.Stat(a.Path(env)); info.Mode().Perm() != 0o600 {
		t.Fatalf("a file with a token must be owner-only: %v", info.Mode())
	}
}

func TestNewFileAndRemove(t *testing.T) {
	env, b := setup(t)
	a := app("cursor")
	os.MkdirAll(filepath.Join(env.Home, ".cursor"), 0o700)
	s := Inspect(a, env, target)
	if !s.Installed || s.Exists {
		t.Fatalf("%+v", s)
	}
	c, _ := Plan(s, target, true)
	if err := c.Write(b, target, env, true); err != nil {
		t.Fatal(err)
	}
	got, _ := os.ReadFile(a.Path(env))
	if !strings.Contains(string(got), `"url": "http://127.0.0.1:8000/mcp"`) {
		t.Fatal(string(got))
	}
	c, _ = Plan(Inspect(a, env, target), target, false)
	if err := c.Write(b, target, env, false); err != nil {
		t.Fatal(err)
	}
	if s := Inspect(a, env, target); len(s.CMF()) != 0 {
		t.Fatal(s.Entries)
	}
}

func TestVSCodeUsesServersKey(t *testing.T) {
	env, b := setup(t)
	a := app("vscode")
	write(t, a.Path(env), `{"servers": {"github": {"type": "http", "url": "https://api.githubcopilot.com/mcp/"}}, "inputs": []}`)
	c, _ := Plan(Inspect(a, env, target), target, true)
	if err := c.Write(b, target, env, true); err != nil {
		t.Fatal(err)
	}
	if s := Inspect(a, env, target); !s.Healthy() || len(s.Entries) != 2 {
		t.Fatalf("%+v", s.Entries)
	}
}

func TestRefusesFileChangedSincePlan(t *testing.T) {
	env, b := setup(t)
	a := app("claude-desktop")
	write(t, a.Path(env), `{"mcpServers": {}}`)
	c, _ := Plan(Inspect(a, env, target), target, true)
	write(t, a.Path(env), `{"mcpServers": {}, "changed": true}`)
	if err := c.Write(b, target, env, true); err == nil || !strings.Contains(err.Error(), "changed since cmf read it") {
		t.Fatal(err)
	}
}

func TestUnreadableConfig(t *testing.T) {
	env, _ := setup(t)
	a := app("antigravity")
	write(t, a.Path(env), `{not json`)
	s := Inspect(a, env, target)
	if s.Err == "" {
		t.Fatal("expected an error")
	}
	if _, err := Plan(s, target, true); err == nil {
		t.Fatal("a file cmf can't read must not be rewritten")
	}
}

func TestPaths(t *testing.T) {
	win := Env{Home: `C:\Users\t`, OS: "windows", AppData: `C:\Users\t\AppData\Roaming`}
	if p := app("claude-desktop").Path(win); !strings.HasSuffix(p, filepath.Join("Claude", "claude_desktop_config.json")) || !strings.HasPrefix(p, win.AppData) {
		t.Fatal(p)
	}
	lin := Env{Home: "/home/t", OS: "linux", Getenv: func(k string) string {
		if k == "CODEX_HOME" {
			return "/opt/codex"
		}
		return ""
	}}
	if p := app("vscode").Path(lin); p != "/home/t/.config/Code/User/mcp.json" {
		t.Fatal(p)
	}
	if p := app("codex").Path(lin); p != "/opt/codex/config.toml" {
		t.Fatal(p)
	}
}

func TestBridgeEntry(t *testing.T) {
	env, b := setup(t)
	a := app("claude-desktop")
	exe := filepath.Join(env.Home, "Applications", "cmf")
	write(t, exe, "binary")
	bt := Target{Port: 8000, Token: "secret-token", Bridge: exe}
	write(t, a.Path(env), `{"mcpServers": {"context-memory-fabric": {"command": "npx", "args": ["-y", "mcp-remote", "http://127.0.0.1:8000/mcp"]}}}`)
	// An existing correct mcp-remote entry is kept as is.
	if c, _ := Plan(Inspect(a, env, bt), bt, true); !c.Empty() {
		t.Fatal(c.Summary)
	}
	os.WriteFile(a.Path(env), []byte(`{"mcpServers": {}}`), 0o644)
	c, _ := Plan(Inspect(a, env, bt), bt, true)
	if err := c.Write(b, bt, env, true); err != nil {
		t.Fatal(err)
	}
	got, _ := os.ReadFile(a.Path(env))
	if !strings.Contains(string(got), `"command": "`+exe+`"`) || !strings.Contains(string(got), `"bridge"`) || strings.Contains(string(got), "secret-token") {
		t.Fatal(string(got)) // the bridge reads the token itself
	}
	if info, _ := os.Stat(a.Path(env)); info.Mode().Perm() != 0o644 {
		t.Fatal("no token in the file, so its mode is kept", info.Mode())
	}
	if s := Inspect(a, env, bt); !s.Healthy() || s.CMF()[0].Kind != "cmf-bridge" {
		t.Fatalf("%+v", s.Entries)
	}
	os.Remove(exe) // the binary moved
	s := Inspect(a, env, bt)
	if s.Healthy() {
		t.Fatal("a bridge whose binary is gone isn't healthy")
	}
	c, _ = Plan(s, Target{Port: 8000, Bridge: filepath.Join(env.Home, "new", "cmf")}, true)
	if !strings.Contains(strings.Join(c.Summary, "; "), "runs a cmf bridge that isn't there any more") {
		t.Fatal(c.Summary)
	}
}
