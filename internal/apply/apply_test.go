package apply

import (
	"context"
	"errors"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/features"
	"github.com/tmargolis/context-memory-fabric/internal/home"
	"github.com/tmargolis/context-memory-fabric/internal/run"
)

type clock struct{ t time.Time }

func (c *clock) now() time.Time        { return c.t }
func (c *clock) sleep(d time.Duration) { c.t = c.t.Add(d) }

func setup(t *testing.T, health func() ([]byte, error)) (Executor, *run.Fake, *[]string) {
	h := home.Home{Dir: t.TempDir()}
	os.WriteFile(h.EnvFile(), []byte("COMPOSE_PROFILES=capture\nGEMINI_API_KEY=g-123456789\n"), 0o600)
	m, err := features.Load()
	if err != nil {
		t.Fatal(err)
	}
	fake := &run.Fake{Answers: map[string]run.Result{"docker compose*": {}}}
	c := &clock{t: time.Date(2026, 10, 24, 12, 0, 0, 0, time.UTC)}
	var log []string
	e := Executor{Home: h, Manifest: m, Runner: fake, Port: 8000, Now: c.now, Sleep: c.sleep,
		HealthTimeout: time.Minute, Log: func(s string) { log = append(log, s) },
		Get: func(context.Context, string) ([]byte, error) { return health() }}
	return e, fake, &log
}

// capturePlan removes the capture profile and stops the poller. No feature
// does that since capture became always on (v0.1.3), so the plan is built by
// hand to keep the stop-and-roll-back path covered.
func capturePlan(t *testing.T, e Executor) *features.Plan {
	env, err := home.ReadEnv(e.Home.EnvFile())
	if err != nil {
		t.Fatal(err)
	}
	p := &features.Plan{Env: env.Clone(), Stop: []string{"cmf-poller"}}
	p.Env.Set("COMPOSE_PROFILES", "")
	return p
}

func TestApplyWritesStopsAndWaits(t *testing.T) {
	calls := 0
	e, fake, _ := setup(t, func() ([]byte, error) {
		calls++
		if calls < 3 {
			return nil, errors.New("connection refused")
		}
		return []byte(`{"status":"ok"}`), nil
	})
	backup, err := e.Apply(context.Background(), capturePlan(t, e), "turn off capture")
	if err != nil {
		t.Fatal(err)
	}
	if data, _ := os.ReadFile(e.Home.EnvFile()); !strings.HasPrefix(string(data), "COMPOSE_PROFILES=\n") {
		t.Fatalf("%q", data)
	}
	if !fake.Ran("docker compose --project-directory "+e.Home.Dir+" stop cmf-poller") || !fake.Ran("docker compose --project-directory "+e.Home.Dir+" up -d") {
		t.Fatalf("%+v", fake.Calls)
	}
	saved, _ := os.ReadFile(backup.Dir() + "/home/.env")
	if !strings.Contains(string(saved), "COMPOSE_PROFILES=capture") || calls != 3 {
		t.Fatal("backup holds the old .env; health polled until ok")
	}
}

func TestApplyRollsBackWhenTheServerDoesNotComeBack(t *testing.T) {
	healthy := false
	e, fake, log := setup(t, func() ([]byte, error) {
		if healthy {
			return []byte(`{"status":"ok"}`), nil
		}
		return nil, errors.New("connection refused")
	})
	// The first up -d "works" but the server never answers; once the old
	// .env is back, it does.
	e.Get = func(context.Context, string) ([]byte, error) {
		env, _ := os.ReadFile(e.Home.EnvFile())
		if strings.Contains(string(env), "COMPOSE_PROFILES=capture") {
			return []byte(`{"status":"ok"}`), nil
		}
		return nil, errors.New("connection refused")
	}
	_, err := e.Apply(context.Background(), capturePlan(t, e), "turn off capture")
	if err == nil || !strings.Contains(err.Error(), "previous settings were restored and the server is back") {
		t.Fatalf("%v\n%s", err, strings.Join(*log, "\n"))
	}
	if data, _ := os.ReadFile(e.Home.EnvFile()); !strings.Contains(string(data), "COMPOSE_PROFILES=capture") {
		t.Fatal("old .env restored")
	}
	ups := 0
	for _, c := range fake.Calls {
		if strings.HasSuffix(c.String(), " up -d") {
			ups++
		}
	}
	if ups != 2 {
		t.Fatalf("up -d after the change and after the rollback, got %d", ups)
	}
}

func TestApplyRollsBackWhenComposeFails(t *testing.T) {
	e, fake, _ := setup(t, func() ([]byte, error) { return []byte(`{"status":"ok"}`), nil })
	fake.Answers["docker compose --project-directory "+e.Home.Dir+" stop cmf-poller"] = run.Result{Err: errors.New("exit status 1"), Stderr: "no such service"}
	_, err := e.Apply(context.Background(), capturePlan(t, e), "turn off capture")
	if err == nil || !strings.Contains(err.Error(), "no such service") || !strings.Contains(err.Error(), "restored") {
		t.Fatal(err)
	}
}
