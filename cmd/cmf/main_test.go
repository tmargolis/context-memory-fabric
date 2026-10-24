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
