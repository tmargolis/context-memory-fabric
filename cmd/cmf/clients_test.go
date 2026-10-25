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
	if !strings.Contains(out.String(), "[FAIL] Each AI app has exactly one CMF entry: Claude Desktop has 2 CMF entries\n") ||
		!strings.Contains(out.String(), "[n/a ] Cursor: not connected · connection no CMF entry") {
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

func TestMigrateNothingToDo(t *testing.T) {
	fake := fakeMachine(t, true)
	fake.Answers["docker info"] = run.Result{}
	userHome = func() (string, error) { return clientEnv().Home, nil }
	t.Cleanup(func() { userHome = os.UserHomeDir })
	var out, errOut bytes.Buffer
	if code := runCLI([]string{"migrate"}, &out, &errOut); code != 0 || !strings.Contains(out.String(), "Nothing to migrate") ||
		!strings.Contains(out.String(), "No repo-based CMF install found") {
		t.Fatalf("code %d\n%s%s", code, out.String(), errOut.String())
	}
	if code := runCLI([]string{"migrate", "restore"}, &out, &errOut); code != 1 || !strings.Contains(errOut.String(), "needs the stamp") {
		t.Fatal(errOut.String())
	}
}

func TestHookStopPrintsJSONAndNeverFails(t *testing.T) {
	fakeMachine(t, true)
	t.Setenv("CMF_NUDGE_MIN_TURNS", "1")
	t.Cleanup(func() { hookStdin = os.Stdin })
	var out, errOut bytes.Buffer
	hookStdin = strings.NewReader(`{"session_id":"s1"}`)
	if code := runCLI([]string{"hook", "stop", "--harness", "claude_code"}, &out, &errOut); code != 0 || !strings.Contains(out.String(), `"decision":"block"`) {
		t.Fatalf("code %d %q", code, out.String())
	}
	out.Reset()
	hookStdin = strings.NewReader(`garbage`)
	if code := runCLI([]string{"hook", "stop", "--harness", "antigravity"}, &out, &errOut); code != 0 || !strings.Contains(out.String(), `"decision":"stop"`) {
		t.Fatalf("code %d %q", code, out.String())
	}
}

func TestBackfill(t *testing.T) {
	fake := fakeMachine(t, true)
	user := clientEnv().Home
	userHome = func() (string, error) { return user, nil }
	t.Cleanup(func() { userHome = os.UserHomeDir })
	os.MkdirAll(filepath.Join(user, ".codex", "sessions"), 0o700)
	os.WriteFile(filepath.Join(user, ".codex", "sessions", "a.jsonl"), []byte(strings.Repeat("x", 4000)), 0o600)
	var out, errOut bytes.Buffer
	if code := runCLI([]string{"backfill", "--days", "7", "--dry-run"}, &out, &errOut); code != 0 ||
		!strings.Contains(out.String(), "Codex") || !strings.Contains(out.String(), "≈ 1k tokens at most") || !strings.Contains(out.String(), "$0") {
		t.Fatalf("code %d\n%s%s", code, out.String(), errOut.String())
	}
	// fakeMachine's stack has no running poller (capture profile, poller down).
	out.Reset()
	if code := runCLI([]string{"backfill", "--yes"}, &out, &errOut); code != 1 || !strings.Contains(errOut.String(), "cmf-poller container isn't running") {
		t.Fatal(errOut.String())
	}
	home := os.Getenv("CMF_HOME")
	fake.Answers["docker compose --project-directory "+home+" ps --all --format json"] = run.Result{Stdout: `{"Name":"cmf-falkordb","Service":"falkordb","State":"running","Health":"healthy"}
{"Name":"cmf-server","Service":"cmf-server","State":"running"}
{"Name":"cmf-poller","Service":"cmf-poller","State":"running"}
`}
	fake.Answers["docker compose --project-directory "+home+" exec -T cmf-poller*"] = run.Result{}
	out.Reset()
	if code := runCLI([]string{"backfill", "--yes", "--source", "codex"}, &out, &errOut); code != 0 {
		t.Fatalf("%s%s", out.String(), errOut.String())
	}
	if !fake.Ran("docker compose --project-directory " + home + " exec -T cmf-poller python -m server.adapters.codex.cli backfill --all --since 2026-10-10T13:00:00Z") {
		t.Fatal(fake.Calls)
	}
	data, _ := os.ReadFile(filepath.Join(home, "state", "backfill.json"))
	if !strings.Contains(string(data), `"result": "done"`) {
		t.Fatal(string(data))
	}
	if code := runCLI([]string{"backfill", "--source", "notes"}, &out, &errOut); code != 1 {
		t.Fatal("unknown source")
	}
}

func TestInstallFresh(t *testing.T) {
	fake := fakeMachine(t, true)
	fake.Answers["docker info"] = run.Result{}
	home := filepath.Join(t.TempDir(), ".cmf")
	t.Setenv("CMF_HOME", home)
	userHome = func() (string, error) { return clientEnv().Home, nil }
	t.Cleanup(func() { userHome = os.UserHomeDir; version = "dev" })
	version = "0.3.0"
	var out, errOut bytes.Buffer
	if code := runCLI([]string{"install", "--yes"}, &out, &errOut); code != 0 {
		t.Fatalf("%s%s", out.String(), errOut.String())
	}
	env, _ := os.ReadFile(filepath.Join(home, ".env"))
	for _, want := range []string{"CMF_VERSION=0.3.0", "FALKORDB_DATABASE=cmf", "COMPOSE_PROFILES=capture"} {
		if !strings.Contains(string(env), want) {
			t.Errorf("%s missing:\n%s", want, env)
		}
	}
	if _, err := os.Stat(filepath.Join(home, "compose.yml")); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "Features") {
		t.Fatal(out.String())
	}
	if code := runCLI([]string{"install", "--yes"}, &out, &errOut); code != 1 || !strings.Contains(errOut.String(), "already installed") {
		t.Fatal(errOut.String())
	}
}
