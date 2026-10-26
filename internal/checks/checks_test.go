package checks

import (
	"context"
	"errors"
	"strings"
	"testing"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/capture"
	"github.com/tmargolis/context-memory-fabric/internal/clients"
	"github.com/tmargolis/context-memory-fabric/internal/features"
	"github.com/tmargolis/context-memory-fabric/internal/mode"
	"github.com/tmargolis/context-memory-fabric/internal/probe"
	"github.com/tmargolis/context-memory-fabric/internal/run"
)

var at = time.Date(2026, 10, 24, 13, 0, 0, 0, time.UTC)

// Status shows local times; tests read them in UTC.
func init() { time.Local = time.UTC }

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
	if !strings.Contains(r["graph"].Detail, "6769 nodes") || r["app-claude-desktop"].Level != OK || !strings.Contains(r["app-claude-desktop"].Detail, "seen        10/24/26 12:00:00 AM") {
		t.Fatalf("%+v %+v", r["graph"], r["app-claude-desktop"])
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

// One row per AI app: on (OK) when its entry works or it called CMF;
// otherwise n/a, saying "off" when the app is on this computer.
func TestAppRows(t *testing.T) {
	f := healthy()
	f.Health.ClientsSeen = nil
	f.Clients = []clients.State{
		{ID: "claude-code", Label: "Claude Code", Path: "/u/.claude.json", Installed: true, Exists: true,
			Entries: []clients.Entry{{Name: "context-memory-fabric", URL: "http://127.0.0.1:8000/mcp", CMF: true, Correct: true, Kind: "url"}}},
		{ID: "cursor", Label: "Cursor", Path: "/u/.cursor/mcp.json", Installed: true},
		{ID: "vscode", Label: "VS Code"},
	}
	f.Capture = []capture.SourceState{
		{Source: capture.Source{ID: "claude_code", EnvKey: "CMF_CAPTURE_CLAUDE_CODE_DIR"}, Present: true, Hook: "plugin", Poller: true,
			Folder: "/u/.claude/projects", Last: at.Add(-3 * time.Minute)},
		{Source: capture.Source{ID: "codex", EnvKey: "CMF_CAPTURE_CODEX_DIR"}, Present: true, Hook: "python", Worker: true},
	}
	r := byID(Evaluate(f))
	code := r["app-claude-code"]
	for _, want := range []string{"connection  local entry in /u/.claude.json", "hook        the cmf-capture plugin", "poller      /u/.claude/projects · last transcript 10/24/26 12:57:00 PM"} {
		if code.Level != OK || !strings.Contains(code.Detail, want) {
			t.Fatalf("%q in %+v", want, code)
		}
	}
	if c := r["app-cursor"]; c.Level != NA || !strings.HasPrefix(c.Detail, "not connected\nconnection  no CMF entry") {
		t.Fatalf("%+v", c)
	}
	if _, shown := r["app-vscode"]; shown {
		t.Fatal("an app that isn't on this computer gets no row")
	}
	if !strings.Contains(r["capture"].Detail, "reading") {
		t.Fatalf("%+v", r["capture"])
	}
	if r["selftest"].Action == nil || r["selftest"].Fix != nil {
		t.Fatalf("the self-test can be run any time: %+v", r["selftest"])
	}

	// A claude.ai connector: Desktop calls CMF with no local entry, so Claude
	// Code (same account) has it too; Chat and Cowork share one row.
	f.Clients = append(f.Clients, clients.State{ID: "claude-desktop", Label: "Claude Desktop", Path: "/u/cfg.json", Installed: true})
	f.Clients[0].Entries = nil
	f.Health.ClientsSeen = []probe.ClientSeen{{Harness: "claude_desktop", LastSeen: at.Add(-time.Hour)}}
	r = byID(Evaluate(f))
	if c := r["app-claude-code"]; c.Level != OK || !strings.Contains(c.Detail, "the claude.ai connector") {
		t.Fatalf("%+v", c)
	}
	if c := r["app-claude-desktop"]; c.Title != "Claude Desktop (Chat and Cowork)" || c.Level != OK || !strings.Contains(c.Detail, "the claude.ai connector (no local entry") {
		t.Fatalf("%+v", c)
	}
	if _, old := r["app-cowork"]; old {
		t.Fatal("Cowork is part of the Claude Desktop row")
	}
	if c := r["app-codex"]; !strings.Contains(c.Detail, "older Python checkpoint hook; plus an older host worker") || !strings.Contains(c.Detail, "CMF_CAPTURE_CODEX_DIR isn't set") {
		t.Fatalf("%+v", c)
	}
	if Worst(Evaluate(f)) != OK {
		t.Fatal("apps that are off don't make Status unhealthy")
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
	if r := byID(Evaluate(f))["providers"]; r.Level != NA || !strings.Contains(r.Detail, "older than") {
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
	if r := byID(Evaluate(f))["selftest"]; r.Level != OK || r.Detail != "passed 10/24/26 10:00:00 AM (74 s)" {
		t.Fatalf("%+v", r)
	}
	f.Health.Selftest = &probe.Selftest{At: at.Add(-72 * time.Hour), FailedStage: "recall", FailedDetail: "search didn't return it"}
	r := byID(Evaluate(f))["selftest"]
	if r.Level != Fail || r.Detail != "recall failed 10/21/26 01:00:00 PM: search didn't return it" || r.Fix == nil ||
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

// Every row has a description and somewhere to go, and each Features target
// exists (a feature id from the manifest, or a group the page renders).
func TestEveryRowHasAboutAndATarget(t *testing.T) {
	m, err := features.Load()
	if err != nil {
		t.Fatal(err)
	}
	ids := map[string]bool{"group:apps": true}
	for _, id := range append(clients.IDs(), "connector") { // the Features page's connector card
		ids["client:"+id] = true
	}
	for _, g := range m.Groups {
		ids["group:"+g] = true
	}
	for _, f := range m.Features {
		ids[f.ID] = true
	}
	for _, r := range Evaluate(healthy()) {
		if r.About == "" {
			t.Errorf("%s has no description", r.ID)
		}
		if r.Feature != "" && !ids[r.Feature] {
			t.Errorf("%s links to %q, which the Features page doesn't have", r.ID, r.Feature)
		}
		if r.Feature == "" && r.Doc == "" && r.ID != "selftest" {
			t.Errorf("%s has nowhere to link", r.ID)
		}
	}
}

func TestConnectorRow(t *testing.T) {
	f := healthy()
	delete(f.Env, "CMF_MCP_ISSUER_URL")
	if r := byID(Evaluate(f))["connector"]; r.Level != NA || !strings.Contains(r.Detail, "needs remote access") {
		t.Fatalf("%+v", r)
	}
	f.Env["CMF_MCP_ISSUER_URL"] = "https://mac.tail1.ts.net/"
	f.Health.ClientsSeen = []probe.ClientSeen{{Harness: "claude_desktop", LastSeen: at.Add(-2 * time.Hour)}}
	if r := byID(Evaluate(f))["connector"]; r.Level != OK || !strings.Contains(r.Detail, "https://mac.tail1.ts.net/mcp") || !strings.Contains(r.Detail, "last call   10/24/26 11:00:00 AM") {
		t.Fatalf("%+v", r)
	}
	f.Health.ClientsSeen = nil
	if r := byID(Evaluate(f))["connector"]; r.Level != NA || !strings.Contains(r.Detail, "Settings → Connectors") {
		t.Fatalf("%+v", r)
	}
}

// An app whose entry looks fine but whose calls the server refuses (a lapsed
// OAuth sign-in, an old token) is flagged, with a one-click switch to this
// computer's address; a successful call since clears it.
func TestRefusedAppRow(t *testing.T) {
	f := healthy()
	f.Self = "/Applications/cmf"
	f.Env["CMF_MCP_ISSUER_URL"] = "https://mac.tail1.ts.net"
	f.Clients = []clients.State{{ID: "codex", Label: "Codex", Path: "/u/.codex/config.toml", Installed: true, Exists: true,
		Entries: []clients.Entry{{Name: "context_memory_fabric", URL: "https://mac.tail1.ts.net/mcp", CMF: true, Correct: true, Kind: "url"}}}}
	f.Health.ClientsSeen = []probe.ClientSeen{{Harness: "claude_desktop", LastSeen: at.Add(-time.Hour)}}
	f.Health.RefusedCalls = []probe.RefusedCall{{Harness: "codex", Count: 12, LastAt: at.Add(-5 * time.Minute), LastPath: "/token"}}
	r := byID(Evaluate(f))
	c := r["app-codex"]
	if c.Level != Warn || !strings.HasPrefix(c.Detail, "CMF refused 12 calls in the last day (last 10/24/26 12:55:00 PM)") || c.Fix == nil ||
		c.Fix.Label != "Use this computer's address" || c.Fix.Steps[0].Show != "/Applications/cmf clients set codex --yes" {
		t.Fatalf("%+v %+v", c, c.Fix)
	}
	if r["connector"].Level != OK {
		t.Fatalf("Codex's refusals aren't the connector's: %+v", r["connector"])
	}
	f.Health.ClientsSeen = append(f.Health.ClientsSeen, probe.ClientSeen{Harness: "codex", LastSeen: at.Add(-time.Minute)})
	if c := byID(Evaluate(f))["app-codex"]; c.Level != OK || c.Fix != nil {
		t.Fatalf("a call that got in since clears it: %+v", c)
	}

	// The connector's own refusals: reconnect it in claude.ai.
	f.Health.RefusedCalls = []probe.RefusedCall{{Harness: "claude_user", Count: 1, LastAt: at.Add(-time.Minute)}}
	if c := byID(Evaluate(f))["connector"]; c.Level != Warn || !strings.Contains(c.Detail, "CMF refused 1 call ") || !strings.Contains(c.Detail, "Disconnect, then Connect") {
		t.Fatalf("%+v", c)
	}
}

func TestRefusalsTheAppGotPastAreIgnored(t *testing.T) {
	f := healthy()
	f.Clients = []clients.State{{ID: "codex", Label: "Codex", Path: "/u/.codex/config.toml", Installed: true, Exists: true,
		Entries: []clients.Entry{{Name: "context_memory_fabric", URL: "http://127.0.0.1:8000/mcp", CMF: true, Correct: true, Kind: "url"}}}}
	ok := at.Add(-4 * time.Minute)
	f.Health.RefusedCalls = []probe.RefusedCall{{Harness: "codex", Count: 3, LastAt: at.Add(-5 * time.Minute), LastPath: "/token", LastOKAt: &ok}}
	if c := byID(Evaluate(f))["app-codex"]; c.Level == Warn {
		t.Fatalf("Codex got in right after: %+v", c)
	}
	early := at.Add(-6 * time.Minute)
	f.Health.RefusedCalls[0].LastOKAt = &early
	if c := byID(Evaluate(f))["app-codex"]; c.Level != Warn {
		t.Fatalf("refused since its last accepted request: %+v", c)
	}
}

func TestRetryableSavesAndSmallContext(t *testing.T) {
	f := healthy()
	f.Health.RetryableWrites = []string{"codex-cmf-017"}
	r := byID(Evaluate(f))["capture"]
	if r.Level != Warn || !strings.Contains(r.Detail, "1 failed memory save(s) kept for a retry") || r.Fix == nil ||
		!strings.HasSuffix(r.Fix.Steps[0].Show, "python -m server.memory_writes retry") {
		t.Fatalf("%+v %+v", r, r.Fix)
	}
	f.Health.LLMContext = &probe.LLMContext{Tokens: 8192, Local: true, RecommendedMin: 16384, Model: "qwen"}
	if p := byID(Evaluate(f))["providers"]; p.Level != Warn || !strings.Contains(p.Detail, "qwen is loaded with a 8192-token context") {
		t.Fatalf("%+v", p)
	}
	f.Health.LLMContext.Tokens = 32768
	if p := byID(Evaluate(f))["providers"]; strings.Contains(p.Detail, "token context") {
		t.Fatalf("%+v", p)
	}
}

func TestCmfAndServerVersionsMustMatch(t *testing.T) {
	f := healthy()
	f.Health.Version = "0.1.4"
	for _, v := range []string{"0.1.4", "v0.1.4", "0.1.4-dev", ""} {
		f.CmfVersion = v
		if r := byID(Evaluate(f))["mode"]; r.Level != OK {
			t.Fatalf("%q: %+v", v, r)
		}
	}
	f.CmfVersion = "0.1.2"
	if r := byID(Evaluate(f))["mode"]; r.Level != Warn || !strings.Contains(r.Detail, "this cmf is 0.1.2 but the server is 0.1.4") {
		t.Fatalf("%+v", r)
	}
}

func TestOutdatedHookCmf(t *testing.T) {
	f := healthy()
	f.Self = "/Applications/cmf-new"
	f.Health.Version = "0.1.5"
	f.Clients = []clients.State{{ID: "codex", Label: "Codex", Path: "/u/.codex/config.toml", Installed: true, Exists: true,
		Entries: []clients.Entry{{Name: "context_memory_fabric", URL: "http://127.0.0.1:8000/mcp", CMF: true, Correct: true, Kind: "url"}}}}
	f.Capture = []capture.SourceState{{Source: capture.Source{ID: "codex", Harness: "codex"}, Present: true, Hook: "cmf",
		HookBinary: "/Applications/cmf", HookVersion: "0.1.3"}}
	c := byID(Evaluate(f))["app-codex"]
	if c.Level != Warn || !strings.HasPrefix(c.Detail, "the hook runs cmf 0.1.3 but the server is 0.1.5") || c.Fix == nil ||
		c.Fix.Steps[0].Show != "/Applications/cmf-new capture hook update --yes" ||
		!strings.Contains(c.Detail, "hook        cmf hook stop (cmf 0.1.3 at /Applications/cmf)") {
		t.Fatalf("%+v %+v", c, c.Fix)
	}
	f.Capture[0].HookVersion = "0.1.5"
	if c := byID(Evaluate(f))["app-codex"]; c.Level != OK || c.Fix != nil {
		t.Fatalf("%+v", c)
	}
}

func TestInstructionsOnAppRows(t *testing.T) {
	f := healthy()
	f.Clients = []clients.State{
		{ID: "codex", Label: "Codex", Path: "/u/.codex/config.toml", Installed: true, Exists: true,
			Entries: []clients.Entry{{Name: "context_memory_fabric", URL: "http://127.0.0.1:8000/mcp", CMF: true, Correct: true, Kind: "url"}}},
		{ID: "claude-desktop", Label: "Claude Desktop", Path: "/u/claude_desktop_config.json", Installed: true, Exists: true},
	}
	f.Health.ClientsSeen = []probe.ClientSeen{{Harness: "claude_desktop", LastSeen: at.Add(-time.Hour)}}
	f.Instructions = map[string]capture.InstructionsFile{"codex": {App: "codex", Path: "/u/.codex/AGENTS.md", Exists: true, State: "current"}}
	f.Health.LiveCaptures = map[string]probe.LiveCapture{"codex": {Count: 3, Last: at.Add(-2 * time.Hour)}}
	r := byID(Evaluate(f))
	if c := r["app-codex"]; c.Level != OK || !strings.Contains(c.Detail, "instructions CMF's instructions in /u/.codex/AGENTS.md") ||
		!strings.Contains(c.Detail, "captures    3 in the last 7 days (latest 10/24/26 11:00:00 AM)") {
		t.Fatalf("%+v", c)
	}
	if c := r["app-claude-desktop"]; !strings.Contains(c.Detail, "no captures from Chat or Cowork in the last 7 days") {
		t.Fatalf("%+v", c)
	}
	f.Instructions["codex"] = capture.InstructionsFile{App: "codex", Path: "/u/.codex/AGENTS.md", Exists: true, State: "partial", Missing: []string{"capture_session"}}
	f.Health.LiveCaptures["claude_cowork"] = probe.LiveCapture{Count: 2, Last: at.Add(-time.Hour)}
	r = byID(Evaluate(f))
	if c := r["app-codex"]; c.Level != Warn || !strings.HasPrefix(c.Detail, "its instructions in /u/.codex/AGENTS.md don't name capture_session") ||
		c.Fix == nil || !strings.HasSuffix(c.Fix.Steps[0].Show, "instructions add codex --yes") {
		t.Fatalf("%+v %+v", c, c.Fix)
	}
	if c := r["app-claude-desktop"]; !strings.Contains(c.Detail, "working: 2 captures in the last 7 days") {
		t.Fatalf("%+v", c)
	}
}

func TestPythonHookGetsReplaceFix(t *testing.T) {
	f := healthy()
	f.Self = "/Applications/cmf"
	f.Clients = []clients.State{{ID: "antigravity", Label: "Antigravity", Path: "/u/.gemini/config/mcp_config.json", Installed: true, Exists: true,
		Entries: []clients.Entry{{Name: "context-memory-fabric", URL: "http://127.0.0.1:8000/mcp", CMF: true, Correct: true, Kind: "cmf-bridge"}}}}
	f.Capture = []capture.SourceState{{Source: capture.Source{ID: "antigravity", Harness: "antigravity"}, Present: true, Hook: "python", Worker: true}}
	c := byID(Evaluate(f))["app-antigravity"]
	if c.Level != Warn || c.Fix == nil || c.Fix.Label != "Replace with the cmf hook" ||
		c.Fix.Steps[0].Show != "/Applications/cmf capture hook install antigravity --yes" {
		t.Fatalf("%+v %+v", c, c.Fix)
	}
}
