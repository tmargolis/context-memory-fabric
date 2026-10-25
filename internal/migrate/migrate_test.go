package migrate

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/home"
	"github.com/tmargolis/context-memory-fabric/internal/run"
)

// machine answers like Docker, FalkorDB and launchctl on a repo-based install.
type machine struct {
	*run.Fake
	saves      int
	nodesAfter string // count(n) on the new stack's FalkorDB
}

func (m *machine) Run(ctx context.Context, c run.Cmd) run.Result {
	s := c.String()
	switch {
	case strings.HasSuffix(s, " LASTSAVE"):
		m.Fake.Calls = append(m.Fake.Calls, c)
		m.saves++
		if m.saves > 2 { // the first read is "before"; the dump lands on the next poll
			return run.Result{Stdout: "200\n"}
		}
		return run.Result{Stdout: "100\n"}
	case strings.Contains(s, " cmf-falkordb redis-cli ") && strings.Contains(s, "GRAPH.RO_QUERY"):
		m.Fake.Calls = append(m.Fake.Calls, c)
		return run.Result{Stdout: "count(n)\n" + m.nodesAfter + "\n"}
	}
	return m.Fake.Run(ctx, c)
}

type fixture struct {
	t     *testing.T
	paths Paths
	repo  string
	m     *machine
	ops   Ops
	logs  []string
}

func newFixture(t *testing.T, kind string) *fixture {
	root := t.TempDir()
	repo := filepath.Join(root, "Dev", "context-memory-fabric")
	userHome := filepath.Join(root, "home")
	f := &fixture{t: t, repo: repo}
	f.paths = Paths{UserHome: userHome, Home: home.Home{Dir: filepath.Join(userHome, ".cmf")}, LaunchAgents: filepath.Join(userHome, "Library", "LaunchAgents")}
	for _, d := range []string{filepath.Join(repo, "server"), filepath.Join(userHome, ".claude", "projects"), f.paths.LaunchAgents} {
		os.MkdirAll(d, 0o700)
	}
	os.WriteFile(filepath.Join(repo, "Dockerfile"), []byte("FROM python\n"), 0o644)
	os.WriteFile(filepath.Join(repo, "server", "mcp.py"), []byte(""), 0o644)
	os.WriteFile(filepath.Join(repo, ".env"), []byte("GEMINI_API_KEY=AIza-real\nFALKORDB_PASSWORD=pw\nFALKORDB_USER=cmf\nCOMPOSE_PROFILES=spark\nSOME_HOST_FILE=/Users/x/notes.txt\n"), 0o600)

	fdb, srv := falkordbContainers[kind], serverContainers[kind]
	a := map[string]run.Result{
		"docker info":                      {},
		"docker inspect -f {{.Id}} " + fdb: {Stdout: "abc\n"},
		`docker inspect -f {{index .Config.Labels "com.docker.compose.project.working_dir"}} ` + fdb:     {Stdout: repo + "\n"},
		"docker inspect -f {{.State.Running}} " + fdb:                                                    {Stdout: "true\n"},
		"docker inspect -f {{json .Mounts}} " + fdb:                                                      {Stdout: `[{"Type":"volume","Name":"old_falkordb_data","Destination":"/data"}]`},
		"docker exec -e REDISCLI_AUTH " + fdb + " redis-cli --no-auth-warning --user cmf CONFIG GET dir": {Stdout: "dir\n/data\n"},
		"docker exec -e REDISCLI_AUTH " + fdb + " redis-cli --no-auth-warning --user cmf GRAPH.RO_QUERY": {},
		"docker exec -e REDISCLI_AUTH " + fdb + " redis-cli --no-auth-warning --user cmf BGSAVE":         {Stdout: "Background saving started\n"},
		"docker cp*":      {},
		"docker stop*":    {},
		"docker start*":   {},
		"launchctl*":      {},
		"docker compose*": {},
	}
	// the node count before the move
	a["docker exec -e REDISCLI_AUTH "+fdb+" redis-cli --no-auth-warning --user cmf GRAPH.RO_QUERY CMF-local MATCH (n) RETURN count(n)"] = run.Result{Stdout: "count(n)\n42\n"}
	a["docker exec -e REDISCLI_AUTH "+fdb+" redis-cli --no-auth-warning --user cmf GRAPH.RO_QUERY default_db MATCH (n) RETURN count(n)"] = run.Result{Stdout: "count(n)\n42\n"}
	if srv != "" {
		a["docker inspect -f {{json .Mounts}} "+srv] = run.Result{Stdout: `[{"Type":"volume","Name":"old_state","Destination":"/data/cmf"}]`}
	}
	// The journal check, before and after the copy.
	a["docker compose --project-directory "+f.paths.Home.Dir+" run --rm --no-deps -T*"] = run.Result{Stdout: "12 ok\n"}
	f.m = &machine{Fake: &run.Fake{Answers: a}, nodesAfter: "42"}
	f.ops = Ops{Runner: f.m, Sleep: func(time.Duration) {}, Now: time.Now, UID: func() string { return "501" },
		Log: func(s string) { f.logs = append(f.logs, s) },
		Get: func(context.Context, string) ([]byte, error) {
			return []byte(`{"status":"ok","graph":"` + map[string]string{KindPreview: "CMF-local", KindHost: "default_db"}[kind] + `","falkordb":{"reachable":true,"graph_exists":true}}`), nil
		}}
	if kind == KindHost {
		os.WriteFile(filepath.Join(f.paths.LaunchAgents, "com.cmf.mcp-server.plist"), []byte("<key>WorkingDirectory</key>\n<string>"+repo+"</string>"), 0o644)
		os.WriteFile(filepath.Join(f.paths.LaunchAgents, "com.cmf.spark-tunnel.plist"), []byte(""), 0o644)
		os.MkdirAll(filepath.Join(repo, "proposals-docs"), 0o700)
		os.WriteFile(filepath.Join(repo, "proposals-docs", "a.md"), []byte("proposal"), 0o644)
		os.MkdirAll(filepath.Join(repo, "imports", "journal"), 0o700)
		os.WriteFile(filepath.Join(repo, "imports", "journal", "journal.db"), []byte("sqlite"), 0o644)
	}
	return f
}

func TestPreviewSurveyAndPlan(t *testing.T) {
	f := newFixture(t, KindPreview)
	s := Look(context.Background(), f.paths, f.ops)
	if len(s.Blockers) > 0 {
		t.Fatal(s.Blockers)
	}
	if s.Kind != KindPreview || s.Repo != f.repo || s.Graph != "CMF-local" || s.Nodes != 42 || s.DataVolume != "old_falkordb_data" || s.StateVolume != "old_state" {
		t.Fatalf("%+v", s)
	}
	plan := strings.Join(Describe(f.paths, s, "20261024-120000"), "\n")
	for _, want := range []string{"preview install", "volume old_state", "FalkorDB volume old_falkordb_data attached as is", "Stop (not remove): cmf-preview-server, cmf-preview-falkordb", `graph "CMF-local" with 42 nodes`, "SOME_HOST_FILE=/Users/x/notes.txt"} {
		if !strings.Contains(plan, want) {
			t.Errorf("plan lacks %q:\n%s", want, plan)
		}
	}
	if strings.Contains(plan, "AIza-real") {
		t.Fatal("the plan must not show keys")
	}
}

func TestPreviewApply(t *testing.T) {
	f := newFixture(t, KindPreview)
	s := Look(context.Background(), f.paths, f.ops)
	rec, err := Apply(context.Background(), f.paths, f.ops, s, "20261024-120000")
	if err != nil {
		t.Fatalf("%v\n%s", err, strings.Join(f.logs, "\n"))
	}
	h := f.paths.Home
	env, _ := home.ReadEnv(h.EnvFile())
	if env.Get("GEMINI_API_KEY") != "AIza-real" || env.Get("CMF_REPO") != f.repo || env.Get("COMPOSE_PROFILES") != "spark,capture" ||
		env.Get("FALKORDB_DATABASE") != "CMF-local" || env.Get("CMF_CAPTURE_CLAUDE_CODE_DIR") == "" {
		t.Fatal(string(env.Bytes()))
	}
	if info, _ := os.Stat(h.EnvFile()); info.Mode().Perm() != 0o600 {
		t.Fatal("~/.cmf/.env must be owner-only")
	}
	if home.Exists(filepath.Join(f.repo, ".env")) || !home.Exists(filepath.Join(f.repo, ".env.migrated-20261024-120000")) {
		t.Fatal("repo .env should be renamed")
	}
	over, _ := os.ReadFile(h.OverrideFile())
	if !strings.Contains(string(over), "name: old_falkordb_data") || !strings.Contains(string(over), "build:") {
		t.Fatal(string(over))
	}
	if !f.m.Ran("docker cp cmf-preview-falkordb:/data/dump.rdb*") || !f.m.Ran("docker stop cmf-preview-server") || !f.m.Ran("docker stop cmf-preview-falkordb") {
		t.Fatal("backup dump or stops missing")
	}
	if !f.m.Ran("docker compose --project-directory " + h.Dir + " run --rm --no-deps -T -v old_state:/old:ro --entrypoint sh cmf-server*") {
		t.Fatal("preview state not copied")
	}
	if !home.Exists(filepath.Join(h.Dir, "state", "imports", journalMarker)) {
		t.Fatal("journal marker missing")
	}
	if rec.Kind != KindPreview || strings.Join(rec.Containers, ",") != "cmf-preview-server,cmf-preview-falkordb" {
		t.Fatalf("%+v", rec)
	}
	if !home.Exists(filepath.Join(h.BackupsDir(), "20261024-120000", MarkerFile)) || !home.Exists(filepath.Join(h.BackupsDir(), "20261024-120000", "repo.env")) {
		t.Fatal("backup record missing")
	}
}

func TestHostApplyDisablesOnlyReplacedJobs(t *testing.T) {
	f := newFixture(t, KindHost)
	s := Look(context.Background(), f.paths, f.ops)
	if len(s.Blockers) > 0 || s.Kind != KindHost || strings.Join(s.StateItems, ",") != "proposals-docs,imports" {
		t.Fatalf("%+v", s)
	}
	if _, err := Apply(context.Background(), f.paths, f.ops, s, "20261024-120000"); err != nil {
		t.Fatalf("%v\n%s", err, strings.Join(f.logs, "\n"))
	}
	if !f.m.Ran("launchctl disable gui/501/com.cmf.mcp-server") || f.m.Ran("launchctl disable gui/501/com.cmf.spark-tunnel") || f.m.Ran("launchctl disable gui/501/com.cmf.capture-poller") {
		t.Fatal("only installed replaced jobs are disabled")
	}
	h := f.paths.Home
	if got, _ := os.ReadFile(filepath.Join(h.Dir, "state", "proposals-docs", "a.md")); string(got) != "proposal" {
		t.Fatal("state not copied")
	}
	if !home.Exists(filepath.Join(h.Dir, "state", "imports", "journal.moved-20261024-120000", "journal.db")) {
		t.Fatal("bind-mount journal should be set aside")
	}
	if !home.Exists(filepath.Join(f.repo, "proposals-docs", "a.md")) {
		t.Fatal("originals stay in the repo")
	}
}

func TestVerifyFailureRestores(t *testing.T) {
	f := newFixture(t, KindPreview)
	f.m.nodesAfter = "0" // the new stack came up on an empty graph
	s := Look(context.Background(), f.paths, f.ops)
	_, err := Apply(context.Background(), f.paths, f.ops, s, "20261024-120000")
	if err == nil || !strings.Contains(err.Error(), "0 nodes after the move") || !strings.Contains(err.Error(), "previous setup was restored") {
		t.Fatal(err)
	}
	h := f.paths.Home
	if !home.Exists(filepath.Join(f.repo, ".env")) || home.Exists(h.EnvFile()) || !home.Exists(h.EnvFile()+".rolled-back-20261024-120000") {
		t.Fatal("env not restored")
	}
	if !f.m.Ran("docker compose --project-directory "+h.Dir+" down") || !f.m.Ran("docker start cmf-preview-falkordb") || !f.m.Ran("docker start cmf-preview-server") {
		t.Fatal("stack not rolled back")
	}
	// FalkorDB starts before the server that depends on it.
	var order []string
	for _, c := range f.m.Calls {
		if strings.HasPrefix(c.String(), "docker start ") {
			order = append(order, strings.TrimPrefix(c.String(), "docker start "))
		}
	}
	if strings.Join(order, ",") != "cmf-preview-falkordb,cmf-preview-server" {
		t.Fatal(order)
	}
}

func TestBlockers(t *testing.T) {
	f := newFixture(t, KindPreview)
	os.MkdirAll(f.paths.Home.Dir, 0o700)
	os.WriteFile(f.paths.Home.EnvFile(), nil, 0o600)
	f.m.Answers["docker exec -e REDISCLI_AUTH cmf-preview-falkordb redis-cli --no-auth-warning --user cmf CONFIG GET dir"] = run.Result{Stdout: "dir\n/var/lib/falkordb/data\n"}
	s := Look(context.Background(), f.paths, f.ops)
	text := strings.Join(s.Blockers, "\n")
	if !strings.Contains(text, "looks migrated already") || !strings.Contains(text, "writes its data to /var/lib/falkordb/data inside the container") {
		t.Fatal(text)
	}
	if _, err := Apply(context.Background(), f.paths, f.ops, s, "x"); !errors.Is(err, errBlocked) {
		t.Fatal(err)
	}

	none := newFixture(t, KindPreview)
	none.m.Answers["docker inspect -f {{.Id}} cmf-preview-falkordb"] = run.Result{Err: errors.New("no such object")}
	if s := Look(context.Background(), none.paths, none.ops); !strings.Contains(strings.Join(s.Blockers, ""), "No repo-based CMF install found") {
		t.Fatal(s.Blockers)
	}
}

func TestRestoreFromRecord(t *testing.T) {
	f := newFixture(t, KindPreview)
	s := Look(context.Background(), f.paths, f.ops)
	if _, err := Apply(context.Background(), f.paths, f.ops, s, "20261024-120000"); err != nil {
		t.Fatal(err)
	}
	f.m.Calls = nil
	if err := Restore(context.Background(), f.paths, f.ops, "20261024-120000"); err != nil {
		t.Fatal(err)
	}
	if !home.Exists(filepath.Join(f.repo, ".env")) || !f.m.Ran("docker start cmf-preview-falkordb") {
		t.Fatal("restore incomplete")
	}
	if err := Restore(context.Background(), f.paths, f.ops, "nope"); err == nil {
		t.Fatal("unknown stamp")
	}
}
