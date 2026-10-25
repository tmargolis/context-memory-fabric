package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/clients"
	"github.com/tmargolis/context-memory-fabric/internal/run"
)

func desktopPath() string {
	a, _ := clients.Find("claude-desktop")
	return a.Path(clientEnv())
}

func TestClientsSetDryRunThenYes(t *testing.T) {
	fakeMachine(t, true)
	os.MkdirAll(filepath.Dir(desktopPath()), 0o700)
	var out, errOut bytes.Buffer
	runCLI([]string{"clients"}, &out, &errOut)
	if !strings.Contains(out.String(), "Claude Desktop  not set up") || !strings.Contains(out.String(), "Cursor          not installed") {
		t.Fatal(out.String())
	}
	out.Reset()
	if code := runCLI([]string{"clients", "set", "claude-desktop", "--dry-run"}, &out, &errOut); code != 0 ||
		!strings.Contains(out.String(), `add "context-memory-fabric" → http://127.0.0.1:8000/mcp`) || !strings.Contains(out.String(), "Dry run") {
		t.Fatalf("code %d\n%s%s", code, out.String(), errOut.String())
	}
	if _, err := os.Stat(desktopPath()); err == nil {
		t.Fatal("dry run wrote the file")
	}
	out.Reset()
	if code := runCLI([]string{"clients", "set", "claude-desktop", "--yes"}, &out, &errOut); code != 0 {
		t.Fatalf("code %d\n%s%s", code, out.String(), errOut.String())
	}
	if !strings.Contains(out.String(), "Quit Claude Desktop completely") || !strings.Contains(out.String(), "Undo with: cmf restore") {
		t.Fatal(out.String())
	}
	stamp := strings.TrimSpace(out.String()[strings.LastIndex(out.String(), "cmf restore ")+len("cmf restore "):])
	out.Reset()
	runCLI([]string{"clients"}, &out, &errOut)
	if !strings.Contains(out.String(), "Claude Desktop  connected") {
		t.Fatal(out.String())
	}
	// Undo: the file didn't exist before, so restoring removes it.
	later := now().Add(time.Minute)
	now = func() time.Time { return later }
	if code := runCLI([]string{"restore", stamp, "--yes"}, &out, &errOut); code != 0 {
		t.Fatalf("restore %s: %s", stamp, errOut.String())
	}
	if _, err := os.Stat(desktopPath()); err == nil {
		t.Fatal("restore should remove the file cmf created")
	}
	if code := runCLI([]string{"clients", "bogus"}, &out, &errOut); code != 1 {
		t.Fatal("unknown verb")
	}
	if code := runCLI([]string{"clients", "set", "notepad"}, &out, &errOut); code != 1 || !strings.Contains(errOut.String(), "unknown app") {
		t.Fatal(errOut.String())
	}
}

func TestClientsFixOnlyTouchesAppsWithEntries(t *testing.T) {
	fakeMachine(t, true)
	os.MkdirAll(filepath.Dir(desktopPath()), 0o700)
	os.WriteFile(desktopPath(), []byte(`{"mcpServers": {"cmf": {"url": "http://127.0.0.1:9999/mcp"}, "context-memory-fabric": {"command": "npx", "args": ["-y", "mcp-remote", "http://127.0.0.1:8000/mcp"]}}}`), 0o644)
	cursor := filepath.Join(clientEnv().Home, ".cursor")
	os.MkdirAll(cursor, 0o700)
	var out, errOut bytes.Buffer
	runCLI([]string{"status"}, &out, &errOut)
	if !strings.Contains(out.String(), "[FAIL] Each AI app has exactly one CMF entry: Claude Desktop has 2 CMF entries; no entry: cursor") ||
		!strings.Contains(out.String(), "") {
		t.Fatal(out.String())
	}
	out.Reset()
	if code := runCLI([]string{"clients", "fix", "--yes"}, &out, &errOut); code != 0 || !strings.Contains(out.String(), `remove "cmf"`) {
		t.Fatalf("code %d\n%s%s", code, out.String(), errOut.String())
	}
	if _, err := os.Stat(filepath.Join(cursor, "mcp.json")); err == nil {
		t.Fatal("fix must not add entries to apps that had none")
	}
	out.Reset()
	runCLI([]string{"clients", "fix"}, &out, &errOut)
	if !strings.Contains(out.String(), "Nothing to fix") {
		t.Fatal(out.String())
	}
}

func TestApplyWithClientsOnly(t *testing.T) {
	fake := fakeMachine(t, false) // Docker down: a clients-only change doesn't need it
	fake.Answers["docker compose*"] = run.Result{}
	os.MkdirAll(filepath.Join(clientEnv().Home, ".cursor"), 0o700)
	file := filepath.Join(t.TempDir(), "c.json")
	os.WriteFile(file, []byte(`{"clients": {"cursor": true}}`), 0o600)
	var out, errOut bytes.Buffer
	if code := runCLI([]string{"apply", file, "--yes"}, &out, &errOut); code != 0 {
		t.Fatalf("code %d\n%s%s", code, out.String(), errOut.String())
	}
	if strings.Contains(out.String(), "docker compose up") || !strings.Contains(out.String(), "Cursor: Reload Cursor.") {
		t.Fatal(out.String())
	}
	got, _ := os.ReadFile(filepath.Join(clientEnv().Home, ".cursor", "mcp.json"))
	if !strings.Contains(string(got), "127.0.0.1:8000/mcp") {
		t.Fatal(string(got))
	}
	os.WriteFile(file, []byte(`{"clients": {"notepad": true}}`), 0o600)
	if code := runCLI([]string{"apply", file, "--yes"}, &out, &errOut); code != 1 || !strings.Contains(out.String(), `unknown AI app "notepad"`) {
		t.Fatal(out.String())
	}
}
