package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/probe"
	"github.com/tmargolis/context-memory-fabric/internal/run"
)

func TestUnknownCommand(t *testing.T) {
	var out, errOut bytes.Buffer
	if code := runCLI([]string{"frobnicate"}, &out, &errOut); code != 2 || !strings.Contains(errOut.String(), "unknown command") {
		t.Fatalf("code %d, %s", code, errOut.String())
	}
}

func TestModeShowAndDryRun(t *testing.T) {
	t.Setenv("CMF_HOME", t.TempDir())
	var out, errOut bytes.Buffer
	if code := runCLI([]string{"mode"}, &out, &errOut); code != 0 || !strings.Contains(out.String(), "Mode: standard (from detected)") {
		t.Fatalf("code %d: %s %s", code, out.String(), errOut.String())
	}
	out.Reset()
	if code := runCLI([]string{"mode", "set", "standard", "--dry-run"}, &out, &errOut); code != 0 || !strings.Contains(out.String(), "Dry run") {
		t.Fatalf("code %d: %s %s", code, out.String(), errOut.String())
	}
	out.Reset()
	if runCLI([]string{"mode"}, &out, &errOut); !strings.Contains(out.String(), "from detected") {
		t.Fatal("dry run must not write mode.json")
	}
}

func fakeMachine(t *testing.T, dockerUp bool) *run.Fake {
	dir := t.TempDir()
	t.Setenv("CMF_HOME", dir)
	os.WriteFile(filepath.Join(dir, ".env"), []byte("COMPOSE_PROFILES=capture\n"), 0o600)
	fake := &run.Fake{Answers: map[string]run.Result{
		"docker compose --project-directory " + dir + " ps --all --format json": {Stdout: `{"Name":"cmf-falkordb","Service":"falkordb","State":"running","Health":"healthy"}
{"Name":"cmf-server","Service":"cmf-server","State":"running"}
`},
		"docker compose --project-directory " + dir + " up -d cmf-poller": {},
		"docker ps --all --format json":                                   {},
		"lsof*":                                                           {Stdout: "p1\nccom.docke\n"},
		"launchctl*":                                                      {},
		"id -u":                                                           {Stdout: "501"},
		"pgrep*":                                                          {Err: errors.New("exit status 1")},
		"docker exec*":                                                    {Stdout: "count(n)\n5\n"},
		"open -a Docker":                                                  {},
	}}
	if dockerUp {
		fake.Answers["docker version --format {{.Server.Version}}"] = run.Result{Stdout: "29.7.2"}
	} else {
		fake.Answers["docker version --format {{.Server.Version}}"] = run.Result{Err: errors.New("exit status 1"), Stderr: "Cannot connect to the Docker daemon"}
	}
	runner, goos = fake, "darwin"
	getHealth = func(context.Context, string) ([]byte, error) {
		return []byte(`{"status":"ok","graph":"g","falkordb":{"reachable":true,"graph_exists":true},"clients_seen":[{"harness":"codex","last_seen":"2026-10-24T12:00:00Z"}]}`), nil
	}
	now = func() time.Time { return time.Date(2026, 10, 24, 13, 0, 0, 0, time.UTC) }
	t.Cleanup(func() { runner, getHealth, goos, now = run.Exec{}, probe.HTTPGet, runtime.GOOS, time.Now })
	return fake
}

func TestDoctorFixStartsThePoller(t *testing.T) {
	fake := fakeMachine(t, true)
	var out, errOut bytes.Buffer
	code := runCLI([]string{"status"}, &out, &errOut)
	if code != 1 || !strings.Contains(out.String(), "[FAIL] CMF server and FalkorDB containers: not running: cmf-poller") {
		t.Fatalf("code %d\n%s%s", code, out.String(), errOut.String())
	}
	out.Reset()
	runCLI([]string{"doctor", "--fix=capture"}, &out, &errOut)
	started := false
	for _, c := range fake.Calls {
		started = started || strings.HasSuffix(c.String(), " up -d cmf-poller")
	}
	if !started || !strings.Contains(out.String(), "Fixing capture: Start the poller") || !strings.Contains(out.String(), "Checking again:") {
		t.Fatalf("%s", out.String())
	}
	if fake.Ran("open -a Docker") {
		t.Fatal("--fix=capture runs only that fix")
	}
}

func TestDoctorJSON(t *testing.T) {
	fakeMachine(t, false)
	var out, errOut bytes.Buffer
	runCLI([]string{"doctor", "--json"}, &out, &errOut)
	var report struct {
		Overall string
		Checks  []struct {
			ID, Level string
			Fix       *struct{ Label string }
		}
	}
	if err := json.Unmarshal(out.Bytes(), &report); err != nil {
		t.Fatalf("%v\n%s", err, out.String())
	}
	if report.Overall != "fail" || report.Checks[1].ID != "docker" || report.Checks[1].Fix == nil || report.Checks[1].Fix.Label != "Start Docker Desktop" {
		t.Fatalf("%+v", report)
	}
}

func TestDoctorRejectsJSONWithFix(t *testing.T) {
	var out, errOut bytes.Buffer
	if code := runCLI([]string{"doctor", "--json", "--fix"}, &out, &errOut); code != 1 {
		t.Fatal(code)
	}
}

func TestApplyDryRunCancelAndYes(t *testing.T) {
	fake := fakeMachine(t, true)
	fake.Answers["docker compose*"] = run.Result{}
	home := os.Getenv("CMF_HOME")
	os.WriteFile(filepath.Join(home, ".env"), []byte("COMPOSE_PROFILES=capture\nGEMINI_API_KEY=g-123456789\n"), 0o600)
	file := filepath.Join(t.TempDir(), "off.json")
	os.WriteFile(file, []byte(`{"features": {"capture": {"enabled": false}}}`), 0o600)
	sleep, healthTimeout = func(time.Duration) {}, time.Second
	t.Cleanup(func() { sleep, healthTimeout, stdin = time.Sleep, 3*time.Minute, os.Stdin })

	var out, errOut bytes.Buffer
	if code := runCLI([]string{"apply", file, "--dry-run"}, &out, &errOut); code != 0 ||
		!strings.Contains(out.String(), "set COMPOSE_PROFILES=empty (was capture)") && !strings.Contains(out.String(), "set COMPOSE_PROFILES= (was capture)") {
		t.Fatalf("code %d\n%s%s", code, out.String(), errOut.String())
	}
	if !strings.Contains(out.String(), "Stop the cmf-poller container") || !strings.Contains(out.String(), "Dry run") {
		t.Fatal(out.String())
	}

	stdin = strings.NewReader("n\n")
	out.Reset()
	if code := runCLI([]string{"apply", file}, &out, &errOut); code != 1 || !strings.Contains(errOut.String(), "cancelled") {
		t.Fatalf("code %d %s", code, errOut.String())
	}
	if data, _ := os.ReadFile(filepath.Join(home, ".env")); !strings.Contains(string(data), "COMPOSE_PROFILES=capture") {
		t.Fatal("cancel writes nothing")
	}

	out.Reset()
	if code := runCLI([]string{"apply", file, "--yes"}, &out, &errOut); code != 0 || !strings.Contains(out.String(), "Undo with: cmf restore 20261024-130000") {
		t.Fatalf("code %d\n%s%s", code, out.String(), errOut.String())
	}
	out.Reset()
	runCLI([]string{"restore"}, &out, &errOut)
	if !strings.Contains(out.String(), "20261024-130000  cmf apply: turn off capture") {
		t.Fatal(out.String())
	}
}

func TestApplyReportsEveryProblem(t *testing.T) {
	fakeMachine(t, true)
	file := filepath.Join(t.TempDir(), "bad.json")
	os.WriteFile(file, []byte(`{"features": {"embeddings": {"provider": "anthropic"}, "teleport": {}}}`), 0o600)
	var out, errOut bytes.Buffer
	if code := runCLI([]string{"apply", file}, &out, &errOut); code != 1 ||
		!strings.Contains(out.String(), "Anthropic has no embeddings API") || !strings.Contains(out.String(), `unknown feature "teleport"`) {
		t.Fatalf("code %d\n%s", code, out.String())
	}
}
