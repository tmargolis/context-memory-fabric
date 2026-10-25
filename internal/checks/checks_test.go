package checks

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/mode"
	"github.com/tmargolis/context-memory-fabric/internal/probe"
	"github.com/tmargolis/context-memory-fabric/internal/run"
)

var at = time.Date(2026, 10, 24, 13, 0, 0, 0, time.UTC)

func healthy() probe.Facts {
	dev := mode.Mode{Mode: mode.Developer, Repo: "/src/cmf"}
	h := &probe.Health{Status: "ok", Graph: "mem-fabric-local"}
	h.FalkorDB.Reachable, h.FalkorDB.GraphExists = true, true
	h.KnowledgeSources = []string{"notion"}
	h.ClientsSeen = []probe.ClientSeen{{Harness: "claude_desktop", LastSeen: at.Add(-13 * time.Hour)}}
	return probe.Facts{
		Installed: true,
		At:        at, OS: "darwin", Home: "/Users/t/.cmf", Port: 8000, Nodes: 6769,
		Mode:   mode.State{Mode: dev, Detected: dev, Source: "detected"},
		Env:    map[string]string{"COMPOSE_PROFILES": "spark,capture"},
		Docker: probe.Docker{Installed: true, Responding: true, Version: "29.7.2"},
		Stack: []probe.Container{
			{Name: "cmf-falkordb", Service: "falkordb", State: "running", Health: "healthy"},
			{Name: "cmf-server", Service: "cmf-server", State: "running"},
			{Name: "cmf-poller", Service: "cmf-poller", State: "running"},
		},
		Strays:   []probe.Container{{Name: "context-memory-fabric-falkordb", State: "exited"}},
		Listener: &probe.Listener{Command: "com.docke", PID: 1},
		Launchd: []probe.LaunchdJob{
			{Label: "com.cmf.mcp-server", Disabled: true},
			{Label: "com.cmf.capture-poller", Disabled: true},
			{Label: "com.cmf.spark-tunnel", Loaded: true, PID: 1822},
		},
		Health: h,
	}
}

func byID(results []Result) map[string]Result {
	m := map[string]Result{}
	for _, r := range results {
		m[r.ID] = r
	}
	return m
}

func TestHealthyInstallIsGreen(t *testing.T) {
	results := Evaluate(healthy())
	if Worst(results) != OK {
		for _, r := range results {
			t.Logf("%s %s %s", r.Level, r.ID, r.Detail)
		}
		t.Fatal("expected all ok / n/a")
	}
	r := byID(results)
	if !strings.Contains(r["graph"].Detail, "6769 nodes") || !strings.Contains(r["apps-seen"].Detail, "claude_desktop (13 h ago)") {
		t.Fatalf("%+v %+v", r["graph"], r["apps-seen"])
	}
	if !strings.Contains(r["launchd"].Detail, "com.cmf.spark-tunnel") {
		t.Fatal("the user's own jobs are listed, not touched")
	}
	for _, id := range []string{"providers", "embedding", "clients", "selftest"} {
		if r[id].Level != NA {
			t.Fatalf("%s should be n/a until its item lands", id)
		}
	}
}

func TestDockerHungOffersRestartAndSkipsDockerRows(t *testing.T) {
	f := healthy()
	f.Docker = probe.Docker{Installed: true, Hung: true}
	f.Health, f.HealthErr = nil, "connection refused"
	r := byID(Evaluate(f))
	if r["docker"].Level != Fail || r["docker"].Fix == nil || !strings.Contains(r["docker"].Fix.Steps[0].Show, "com.docker.backend") {
		t.Fatalf("%+v", r["docker"])
	}
	if r["stack"].Level != NA || r["containers"].Level != NA || r["port"].Level != NA {
		t.Fatal("docker-dependent rows wait for Docker")
	}
	if r["graph"].Fix != nil {
		t.Fatal("no restart offered while Docker is down")
	}
}

func TestDockerNotRunning(t *testing.T) {
	f := healthy()
	f.Docker = probe.Docker{Installed: true, Error: "Cannot connect"}
	if fix := byID(Evaluate(f))["docker"].Fix; fix == nil || fix.Steps[0].Show != "open -a Docker" {
		t.Fatalf("%+v", fix)
	}
}

func TestStackDownAndUnhealthy(t *testing.T) {
	f := healthy()
	f.Stack = f.Stack[:2] // no poller
	r := byID(Evaluate(f))
	if r["stack"].Level != Fail || !strings.Contains(r["stack"].Fix.Steps[0].Show, "up -d") {
		t.Fatalf("%+v", r["stack"])
	}
	if r["capture"].Level != Fail {
		t.Fatal("capture needs the poller")
	}

	f = healthy()
	f.Stack[0].Health = "unhealthy"
	r = byID(Evaluate(f))
	if got := r["stack"].Fix.Steps[0].Show; got != "docker compose --project-directory /Users/t/.cmf up -d --force-recreate falkordb" {
		t.Fatal(got)
	}
}

func TestLaunchdConflictIsDisabledNotRemoved(t *testing.T) {
	f := healthy()
	f.Launchd[0] = probe.LaunchdJob{Label: "com.cmf.mcp-server", Loaded: true, PID: 99}
	r := byID(Evaluate(f))["launchd"]
	if r.Level != Fail || len(r.Fix.Steps) != 1 {
		t.Fatalf("%+v", r)
	}
	cmd := r.Fix.Steps[0].Show
	if !strings.Contains(cmd, "bootout gui/$(id -u)/com.cmf.mcp-server") || !strings.Contains(cmd, "launchctl disable") || strings.Contains(cmd, "rm ") {
		t.Fatal(cmd)
	}
}

func TestStrayContainerIsStoppedNotRemoved(t *testing.T) {
	f := healthy()
	f.Strays = append(f.Strays, probe.Container{Name: "cmf-preview-server", State: "running"})
	r := byID(Evaluate(f))["containers"]
	if r.Level != Warn || r.Fix.Steps[0].Show != "docker stop cmf-preview-server" {
		t.Fatalf("%+v", r)
	}
}

func TestPortTakenBySomethingElse(t *testing.T) {
	f := healthy()
	f.Listener = &probe.Listener{Command: "Python", PID: 4242}
	r := byID(Evaluate(f))["port"]
	if r.Level != Fail || !strings.Contains(r.Detail, "Python (PID 4242)") || r.Fix != nil {
		t.Fatalf("%+v", r)
	}
	f.Listener = nil
	if byID(Evaluate(f))["port"].Level != Fail {
		t.Fatal("nothing listening")
	}
}

func TestHostServerIsReportedNotKilled(t *testing.T) {
	f := healthy()
	f.HostMCP = []probe.Process{{PID: 777, Command: "python -m server.mcp"}}
	r := byID(Evaluate(f))["host-server"]
	if r.Level != Warn || r.Fix != nil || !strings.Contains(r.Detail, "PID 777") {
		t.Fatalf("%+v", r)
	}
}

func TestHealthMissingOffersServerRestart(t *testing.T) {
	f := healthy()
	f.Health, f.HealthErr = nil, "connection refused"
	r := byID(Evaluate(f))
	if r["graph"].Level != Fail || r["graph"].Fix.Steps[0].Show != "docker compose --project-directory /Users/t/.cmf restart cmf-server" {
		t.Fatalf("%+v", r["graph"])
	}
}

func TestFailedBackgroundWritesWarn(t *testing.T) {
	f := healthy()
	f.Health.BackgroundWrites = []probe.BackgroundWrite{{Status: "failed", ErrorKind: "embedding_mismatch"}, {Status: "committed"}, {Status: "failed", ErrorKind: "provider_quota"}}
	r := byID(Evaluate(f))["capture"]
	if r.Level != Warn || !strings.Contains(r.Detail, "2 of the last 3 background writes failed, latest: embedding_mismatch") {
		t.Fatalf("%+v", r)
	}
}

func TestNoAppsSeenWarns(t *testing.T) {
	f := healthy()
	f.Health.ClientsSeen[0].LastSeen = at.Add(-30 * time.Hour)
	if byID(Evaluate(f))["apps-seen"].Level != Warn {
		t.Fatal("stale client")
	}
}

func TestCaptureOff(t *testing.T) {
	f := healthy()
	f.Env["COMPOSE_PROFILES"] = "spark"
	f.Stack = f.Stack[:2]
	r := byID(Evaluate(f))
	if r["capture"].Level != Warn || r["stack"].Level != OK {
		t.Fatalf("%+v %+v", r["capture"], r["stack"])
	}
}

func TestApplyFixStopsAtFirstFailure(t *testing.T) {
	fake := &run.Fake{Answers: map[string]run.Result{"a": {}, "b": {Err: errors.New("exit status 1"), Stderr: "boom"}}}
	fix := &Fix{Steps: []Step{step("a", run.Cmd{Name: "a"}), step("b", run.Cmd{Name: "b"}), step("c", run.Cmd{Name: "c"})}}
	err := ApplyFix(context.Background(), fake, fix, func(string) {})
	if err == nil || !strings.Contains(err.Error(), "boom") || fake.Ran("c") {
		t.Fatalf("%v %+v", err, fake.Calls)
	}
}

func TestProviderRow(t *testing.T) {
	f := healthy()
	if r := byID(Evaluate(f))["providers"]; r.Level != NA || !strings.Contains(r.Detail, "predates") {
		t.Fatalf("old server: %+v", r)
	}
	f.Health.ProviderTests = []probe.ProviderTest{
		{Role: "LLM", Provider: "local", OK: true, Detail: "answers"},
		{Role: "embeddings", Provider: "local", OK: true, Detail: "answers"},
	}
	if r := byID(Evaluate(f))["providers"]; r.Level != OK || r.Detail != "LLM local, embeddings local answer" {
		t.Fatalf("%+v", r)
	}
	f.Health.ConfigCheck = &probe.ConfigCheck{Warnings: []string{"capture key missing"}}
	if r := byID(Evaluate(f))["providers"]; r.Level != Warn || r.Detail != "capture key missing" {
		t.Fatalf("%+v", r)
	}
	f.Health.ProviderTests[1] = probe.ProviderTest{Role: "embeddings", Provider: "openai", Detail: "openai rejected the API key (OPENAI_API_KEY)"}
	if r := byID(Evaluate(f))["providers"]; r.Level != Fail || r.Detail != "embeddings openai: openai rejected the API key (OPENAI_API_KEY)" {
		t.Fatalf("%+v", r)
	}
}

func TestEmbeddingRow(t *testing.T) {
	f := healthy()
	f.Health.Graph = "g"
	if r := byID(Evaluate(f))["embedding"]; r.Level != NA {
		t.Fatalf("old server: %+v", r)
	}
	w := 768
	f.Health.Embedding = &probe.Embedding{Configured: 768}
	if r := byID(Evaluate(f))["embedding"]; r.Level != OK || !strings.Contains(r.Detail, "no vectors yet") {
		t.Fatalf("%+v", r)
	}
	f.Health.Embedding.Graph = &w
	if r := byID(Evaluate(f))["embedding"]; r.Level != OK {
		t.Fatalf("%+v", r)
	}
	f.Health.Embedding.Configured = 1024
	if r := byID(Evaluate(f))["embedding"]; r.Level != Fail || !strings.Contains(r.Detail, `EMBEDDING_DIM is 1024 but graph "g" holds 768-wide vectors`) {
		t.Fatalf("%+v", r)
	}
}

func TestRestartingServerPointsAtLogs(t *testing.T) {
	f := healthy()
	for i := range f.Stack {
		if f.Stack[i].Service == "cmf-server" {
			f.Stack[i].State = "restarting"
		}
	}
	if r := byID(Evaluate(f))["stack"]; r.Level != Fail || !strings.Contains(r.Detail, "cmf logs cmf-server --errors") {
		t.Fatalf("%+v", r)
	}
}

func TestSelftestRow(t *testing.T) {
	f := healthy()
	if r := byID(Evaluate(f))["selftest"]; r.Level != NA || !strings.Contains(r.Detail, "cmf selftest") {
		t.Fatalf("%+v", r)
	}
	f.Health.Selftest = &probe.Selftest{OK: true, At: at.Add(-3 * time.Hour), Seconds: 74}
	if r := byID(Evaluate(f))["selftest"]; r.Level != OK || r.Detail != "passed 3 h ago (74 s)" {
		t.Fatalf("%+v", r)
	}
	f.Health.Selftest = &probe.Selftest{At: at.Add(-72 * time.Hour), FailedStage: "recall", FailedDetail: "search didn't return it"}
	r := byID(Evaluate(f))["selftest"]
	if r.Level != Fail || r.Detail != "recall failed 3 days ago: search didn't return it" || r.Fix == nil ||
		!strings.HasSuffix(r.Fix.Steps[0].Show, "exec -T cmf-server python -m server.selftest") {
		t.Fatalf("%+v", r)
	}
}

func TestNotInstalledOffersInstall(t *testing.T) {
	f := healthy()
	f.Installed, f.Self = false, "/Applications/cmf"
	r := byID(Evaluate(f))["mode"]
	if r.Level != Fail || !strings.Contains(r.Detail, "isn't installed") || r.Fix == nil || r.Fix.Steps[0].Show != "/Applications/cmf install --yes" {
		t.Fatalf("%+v", r)
	}
}
