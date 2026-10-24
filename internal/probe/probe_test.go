package probe

import (
	"context"
	"errors"
	"os"
	"testing"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/home"
	"github.com/tmargolis/context-memory-fabric/internal/run"
)

// Formats as captured from Todd's Mac on 2026-10-24 (Docker Desktop 4.88,
// compose 5.4), trimmed to the fields the probe reads.
const composePS = `{"Health":"healthy","Labels":"com.docker.compose.project=cmf,com.docker.compose.service=falkordb","Name":"cmf-falkordb","Names":"cmf-falkordb","Project":"cmf","Service":"falkordb","State":"running"}
{"Health":"","Name":"cmf-server","Project":"cmf","Service":"cmf-server","State":"running"}
{"Health":"","Name":"cmf-poller","Project":"cmf","Service":"cmf-poller","State":"running"}
`

const dockerPS = `{"Labels":"com.docker.compose.project=cmf,com.docker.compose.service=cmf-server","Names":"cmf-server","State":"running"}
{"Labels":"com.docker.compose.project=context-memory-fabric,com.docker.compose.service=falkordb","Names":"context-memory-fabric-falkordb","State":"exited"}
{"Labels":"com.docker.compose.project=cmf-preview,com.docker.compose.service=cmf-server","Names":"cmf-preview-server","State":"running"}
{"Labels":"","Names":"postgres","State":"running"}
`

const launchctlList = "PID\tStatus\tLabel\n-\t0\tcom.cmf.endpoint-probe\n851\t0\tcom.apple.cmfsyncagent\n1822\t0\tcom.cmf.spark-tunnel\n"

const printDisabled = `disabled services = {
		"com.cmf.mcp-server" => disabled
		"com.cmf.capture-poller" => disabled
		"com.apple.something" => enabled
	}
`

const healthJSON = `{"status":"ok","version":"0.1.0","runtime":"docker","graph":"mem-fabric-local",
"falkordb":{"address":"falkordb:6379","reachable":true,"graph_exists":true},
"providers":{"llm":"local","embeddings":"local","capture_llm":"local","embedding_dim":768},
"knowledge_sources":["notion"],"background_writes":[],
"clients_seen":[{"harness":"claude_desktop","last_seen":"2026-10-24T00:13:25.617457+00:00","calls":8,"client_version":"1.0.0"}]}`

func healthyFake(h home.Home) *run.Fake {
	return &run.Fake{Answers: map[string]run.Result{
		"docker version --format {{.Server.Version}}":                             {Stdout: "29.7.2\n"},
		"docker compose --project-directory " + h.Dir + " ps --all --format json": {Stdout: composePS},
		"docker ps --all --format json":                                           {Stdout: dockerPS},
		"lsof -nP -iTCP:8000 -sTCP:LISTEN -Fpc":                                   {Stdout: "p45964\nccom.docke\nf144\n"},
		"launchctl list":                                                          {Stdout: launchctlList},
		"id -u":                                                                   {Stdout: "501\n"},
		"launchctl print-disabled gui/501":                                        {Stdout: printDisabled},
		"pgrep -fl server\\.mcp":                                                  {Err: errors.New("exit status 1")},
		"docker exec -e REDISCLI_AUTH cmf-falkordb redis-cli --no-auth-warning --user todd GRAPH.QUERY mem-fabric-local MATCH (n) RETURN count(n)": {
			Stdout: "count(n)\n6769\nCached execution: 0\nQuery internal execution time: 1.2 milliseconds\n"},
	}}
}

func newHome(t *testing.T) home.Home {
	h := home.Home{Dir: t.TempDir()}
	os.WriteFile(h.EnvFile(), []byte("FALKORDB_USER=todd\nFALKORDB_PASSWORD=secret\nCOMPOSE_PROFILES=spark\nCOMPOSE_PROFILES=spark,capture\n"), 0o600)
	return h
}

func prober(h home.Home, fake *run.Fake, health string, healthErr error) Prober {
	return Prober{Home: h, Runner: fake, OS: "darwin",
		Now: func() time.Time { return time.Date(2026, 10, 24, 13, 0, 0, 0, time.UTC) },
		Get: func(context.Context, string) ([]byte, error) { return []byte(health), healthErr }}
}

func TestCollectHealthyMac(t *testing.T) {
	h := newHome(t)
	fake := healthyFake(h)
	f := prober(h, fake, healthJSON, nil).Collect(context.Background())

	if !f.Docker.Responding || f.Docker.Version != "29.7.2" {
		t.Fatalf("docker %+v", f.Docker)
	}
	if len(f.Stack) != 3 || f.Service("falkordb").Health != "healthy" {
		t.Fatalf("stack %+v", f.Stack)
	}
	if len(f.Strays) != 2 || f.Strays[0].Name != "context-memory-fabric-falkordb" || f.Strays[1].State != "running" {
		t.Fatalf("strays %+v", f.Strays)
	}
	if f.Listener == nil || f.Listener.PID != 45964 || !f.Listener.IsDocker() {
		t.Fatalf("listener %+v", f.Listener)
	}
	if len(f.Launchd) != 4 {
		t.Fatalf("launchd %+v", f.Launchd)
	}
	for _, job := range f.Launchd {
		if job.Label == "com.cmf.mcp-server" && (!job.Disabled || job.Loaded) {
			t.Fatalf("mcp-server should be disabled and unloaded: %+v", job)
		}
		if job.Label == "com.cmf.spark-tunnel" && (!job.Loaded || job.PID != 1822) {
			t.Fatalf("spark-tunnel %+v", job)
		}
	}
	if !f.HasProfile("capture") {
		t.Fatal("last COMPOSE_PROFILES wins")
	}
	if f.Health == nil || f.Health.Graph != "mem-fabric-local" || f.Nodes != 6769 {
		t.Fatalf("health %+v nodes %d %s", f.Health, f.Nodes, f.NodesErr)
	}
	if _, kept := f.Env["FALKORDB_PASSWORD"]; kept {
		t.Fatal("the password must not stay in Facts")
	}
	for _, c := range fake.Calls {
		for _, a := range c.Args {
			if a == "secret" {
				t.Fatalf("password on a command line: %s", c)
			}
		}
		if c.Name == "docker" && len(c.Args) > 0 && c.Args[0] == "exec" && (len(c.Env) != 1 || c.Env[0] != "REDISCLI_AUTH=secret") {
			t.Fatalf("password goes through the environment: %+v", c.Env)
		}
	}
}

func TestCollectDockerHung(t *testing.T) {
	h := newHome(t)
	fake := healthyFake(h)
	fake.Answers["docker version --format {{.Server.Version}}"] = run.Result{Err: run.ErrTimeout}
	f := prober(h, fake, "", errors.New("connection refused")).Collect(context.Background())
	if !f.Docker.Installed || !f.Docker.Hung || f.Docker.Responding {
		t.Fatalf("%+v", f.Docker)
	}
	if fake.Ran("docker compose*") || fake.Ran("docker ps*") || fake.Ran("docker exec*") {
		t.Fatal("no more docker calls once the daemon is hung")
	}
	if f.Health != nil || f.HealthErr == "" {
		t.Fatal("health error recorded")
	}
}

func TestCollectNoDocker(t *testing.T) {
	h := newHome(t)
	fake := healthyFake(h)
	fake.Answers["docker version --format {{.Server.Version}}"] = run.Result{Err: run.ErrNotFound}
	f := prober(h, fake, healthJSON, nil).Collect(context.Background())
	if f.Docker.Installed || f.Nodes != -1 {
		t.Fatalf("%+v", f.Docker)
	}
}

func TestParseRowsArray(t *testing.T) {
	rows := parseRows(`[{"Name":"a","Service":"falkordb","State":"running"}]`)
	if len(rows) != 1 || rows[0].Service != "falkordb" {
		t.Fatalf("%+v", rows)
	}
}
