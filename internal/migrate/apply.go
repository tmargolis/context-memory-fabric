package migrate

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/tmargolis/context-memory-fabric/deploy/docker"
	"github.com/tmargolis/context-memory-fabric/internal/home"
	"github.com/tmargolis/context-memory-fabric/internal/mode"
	"github.com/tmargolis/context-memory-fabric/internal/probe"
	"github.com/tmargolis/context-memory-fabric/internal/run"
)

// Record is backups/<stamp>/migrate.json: what restore needs to undo it.
type Record struct {
	Stamp      string   `json:"stamp"`
	Kind       string   `json:"kind"`
	Repo       string   `json:"repo"`
	Graph      string   `json:"graph"`
	Nodes      int      `json:"nodes"`
	Containers []string `json:"containers"` // stopped by the migration, started again by restore
	Jobs       []string `json:"jobs"`       // launchd jobs disabled
}

func (o Ops) check(ctx context.Context, cmd run.Cmd) error {
	if r := o.Runner.Run(ctx, cmd); r.Err != nil {
		return fmt.Errorf("`%s` failed: %s", cmd, strings.TrimSpace(r.Output()+" "+errText(r.Err)))
	}
	return nil
}

func compose(h home.Home, args ...string) run.Cmd {
	return run.Cmd{Name: "docker", Args: append([]string{"compose", "--project-directory", h.Dir}, args...), Timeout: 15 * time.Minute}
}

func copyTree(src, dst string) error {
	return filepath.Walk(src, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(src, path)
		target := filepath.Join(dst, rel)
		switch {
		case info.IsDir():
			return os.MkdirAll(target, 0o700)
		case info.Mode()&os.ModeSymlink != 0:
			link, err := os.Readlink(path)
			if err != nil {
				return err
			}
			return os.Symlink(link, target)
		}
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		return os.WriteFile(target, data, info.Mode().Perm())
	})
}

func copyPath(src, dst string) error {
	if err := os.MkdirAll(filepath.Dir(dst), 0o700); err != nil {
		return err
	}
	if isDir(src) {
		return copyTree(src, dst)
	}
	data, err := os.ReadFile(src)
	if err != nil {
		return err
	}
	info, _ := os.Stat(src)
	return os.WriteFile(dst, data, info.Mode().Perm())
}

// homeEnv is the repo .env as ~/.cmf/.env: the review rules setting retired,
// the managed settings set (last value wins, so they go at the end).
func homeEnv(s *Survey, stamp string) []byte {
	lines := strings.Split(strings.TrimRight(string(s.RepoEnv.Bytes()), "\n"), "\n")
	if s.Rules != "" {
		for i, l := range lines {
			if strings.HasPrefix(strings.TrimSpace(l), rulesKey+"=") {
				lines[i] = "# " + l + "  # moved to state/" + rulesFile + " by cmf migrate (" + stamp + ")"
			}
		}
	}
	var profiles []string
	for _, p := range strings.Split(s.RepoEnv.Get("COMPOSE_PROFILES"), ",") {
		if p = strings.TrimSpace(p); p != "" {
			profiles = append(profiles, p)
		}
	}
	if !contains(profiles, "capture") {
		profiles = append(profiles, "capture")
	}
	lines = append(lines, "", "# --- added by cmf migrate ("+stamp+") ---", "CMF_REPO="+s.Repo, "COMPOSE_PROFILES="+strings.Join(profiles, ","))
	if s.Kind == KindPreview && s.RepoEnv.Get("FALKORDB_DATABASE") == "" {
		lines = append(lines, "FALKORDB_DATABASE="+s.Graph) // the preview compose file's default
	}
	for _, k := range captureOrder {
		if v, ok := s.CaptureDirs[k]; ok {
			lines = append(lines, k+"="+v)
		}
	}
	return []byte(strings.Join(lines, "\n") + "\n")
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

func override(s *Survey) []byte {
	return []byte(strings.TrimRight(string(docker.ComposeDev), "\n") + "\n\n" +
		"# Added by cmf migrate: the pre-migration FalkorDB volume, attached as is (no data copy).\n" +
		"volumes:\n  falkordb_data:\n    external: true\n    name: " + s.DataVolume + "\n")
}

// bgsave makes FalkorDB write its dump and waits for it.
func (o Ops) bgsave(ctx context.Context, s *Survey) error {
	before := strings.TrimSpace(o.redis(ctx, s, "LASTSAVE").Stdout)
	if r := o.redis(ctx, s, "BGSAVE"); r.Err != nil {
		return fmt.Errorf("FalkorDB BGSAVE: %s", strings.TrimSpace(r.Output()))
	}
	for i := 0; i < 300; i++ {
		if now := strings.TrimSpace(o.redis(ctx, s, "LASTSAVE").Stdout); now != "" && now != before {
			return nil
		}
		o.Sleep(time.Second)
	}
	return fmt.Errorf("FalkorDB BGSAVE didn't finish within 5 minutes")
}

// The journal check, run in the cmf-server image (no Python on the host).
const journalStats = `import sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute('PRAGMA wal_checkpoint(TRUNCATE)') if sys.argv[2] == 'rw' else None; print(c.execute('SELECT count(*) FROM events').fetchone()[0], c.execute('PRAGMA quick_check').fetchone()[0])`

func lastLine(s string) string {
	lines := strings.Split(strings.TrimSpace(s), "\n")
	return strings.TrimSpace(lines[len(lines)-1])
}

// fillJournal copies the old journal into the cmf-journal volume and checks
// it arrived whole. src is a host folder (host kind) or the old state volume
// (preview kind, mounted at /old).
func (o Ops) fillJournal(ctx context.Context, h home.Home, s *Survey) (string, error) {
	base := []string{"run", "--rm", "--no-deps", "-T"}
	var mountArgs []string
	var srcDB, copyFrom string
	switch s.Kind {
	case KindHost:
		src := filepath.Join(h.Dir, "state", "imports", "journal")
		if !isFile(filepath.Join(src, "journal.db")) {
			return "", nil
		}
		mountArgs, srcDB, copyFrom = []string{"-v", src + ":/src"}, "/src/journal.db", "/src"
	case KindPreview:
		mountArgs, srcDB, copyFrom = []string{"-v", s.StateVolume + ":/old"}, "/old/imports/journal/journal.db", "/old/imports/journal"
	}
	py := func(db, rw string) run.Cmd {
		return compose(h, append(append(append([]string{}, base...), mountArgs...), "--entrypoint", "python", "cmf-server", "-c", journalStats, db, rw)...)
	}
	r := o.Runner.Run(ctx, py(srcDB, "rw"))
	if r.Err != nil {
		if strings.Contains(r.Output(), "unable to open database") || strings.Contains(r.Output(), "no such table") {
			return "", nil // no journal yet
		}
		return "", fmt.Errorf("reading the old journal: %s", strings.TrimSpace(r.Output()))
	}
	want := lastLine(r.Stdout)
	if !strings.HasSuffix(want, " ok") {
		return "", fmt.Errorf("the old journal fails quick_check (%s); not copying it", want)
	}
	script := fmt.Sprintf("if [ -e %[1]s/journal.db ]; then echo '%[2]s already holds a journal.db' >&2; exit 3; fi; cp -a %[3]s/. %[1]s/", inVolume, JournalVolume, copyFrom)
	if err := o.check(ctx, compose(h, append(append(append([]string{}, base...), mountArgs...), "--entrypoint", "sh", "cmf-server", "-c", script)...)); err != nil {
		return "", err
	}
	r = o.Runner.Run(ctx, py(inVolume+"/journal.db", "ro"))
	if got := lastLine(r.Stdout); r.Err != nil || got != want {
		return "", fmt.Errorf("the journal in %s reports %q, expected %q", JournalVolume, got, want)
	}
	return want, nil
}

// Apply migrates. On any failure after the backup it restores and returns the error.
func Apply(ctx context.Context, p Paths, o Ops, s *Survey, stamp string) (*Record, error) {
	if len(s.Blockers) > 0 {
		return nil, fmt.Errorf("%w: %s", errBlocked, strings.Join(s.Blockers, " "))
	}
	h := p.Home
	backup := filepath.Join(h.BackupsDir(), stamp)
	rec := &Record{Stamp: stamp, Kind: s.Kind, Repo: s.Repo, Graph: s.Graph, Nodes: s.Nodes}

	// 1. Back up.
	o.Log("[1/5] Backing up into " + backup)
	if err := os.MkdirAll(backup, 0o700); err != nil {
		return nil, err
	}
	steps := []struct{ src, dst string }{{filepath.Join(s.Repo, ".env"), "repo.env"}}
	for _, item := range s.StateItems {
		steps = append(steps, struct{ src, dst string }{filepath.Join(s.StateRoot, item), filepath.Join("state", item)})
	}
	if s.Rules != "" {
		steps = append(steps, struct{ src, dst string }{s.Rules, rulesFile})
	}
	for _, pl := range s.Plists {
		steps = append(steps, struct{ src, dst string }{pl, filepath.Join("launchd", filepath.Base(pl))})
	}
	for _, st := range steps {
		if err := copyPath(st.src, filepath.Join(backup, st.dst)); err != nil {
			return nil, fmt.Errorf("backing up %s: %w", st.src, err)
		}
	}
	if err := o.bgsave(ctx, s); err != nil {
		return nil, err
	}
	dir := s.DataDir
	if dir == "" {
		dir = "/data"
	}
	if err := o.check(ctx, run.Cmd{Name: "docker", Args: []string{"cp", s.Falkordb + ":" + dir + "/dump.rdb", filepath.Join(backup, "falkordb-dump.rdb")}, Timeout: 10 * time.Minute}); err != nil {
		return nil, err
	}
	o.run(ctx, "docker", "cp", s.Falkordb+":"+dir+"/users.acl", filepath.Join(backup, "falkordb-users.acl"))
	if s.Kind == KindPreview && s.StateVolume != "" {
		rec.Containers = append(rec.Containers, serverContainers[KindPreview])
	}
	writeRecord := func() error {
		data, _ := json.MarshalIndent(rec, "", "  ")
		return home.WriteFile(filepath.Join(backup, MarkerFile), data, 0o600)
	}
	if err := writeRecord(); err != nil {
		return nil, err
	}
	os.WriteFile(filepath.Join(backup, "NOTE"), []byte(fmt.Sprintf("cmf migrate: %s install from %s, graph %s (%d nodes). Undo with `cmf migrate restore %s`.\n", s.Kind, s.Repo, s.Graph, s.Nodes, stamp)), 0o600)

	done := map[string]bool{}
	fail := func(err error) (*Record, error) {
		o.Log("\nFailed: " + err.Error() + "\nRestoring the previous setup.")
		undo(ctx, p, o, rec, done)
		return rec, fmt.Errorf("%w; the previous setup was restored (backups kept in %s)", err, backup)
	}

	// 2. Config and state.
	o.Log("[2/5] Writing " + h.EnvFile() + " and copying state")
	for _, item := range s.StateItems {
		if err := copyPath(filepath.Join(s.StateRoot, item), filepath.Join(h.Dir, "state", item)); err != nil {
			return fail(err)
		}
	}
	if s.Rules != "" {
		if err := copyPath(s.Rules, filepath.Join(h.Dir, "state", rulesFile)); err != nil {
			return fail(err)
		}
	}
	done["env"] = true // before writing, so a partial write is undone
	if err := home.WriteFile(h.EnvFile(), homeEnv(s, stamp), 0o600); err != nil {
		return fail(err)
	}
	if err := os.Rename(filepath.Join(s.Repo, ".env"), filepath.Join(s.Repo, ".env.migrated-"+stamp)); err != nil {
		return fail(err)
	}

	// 3. Compose files and mode.
	o.Log("[3/5] Writing the compose files (Developer mode)")
	modeJSON, _ := json.MarshalIndent(mode.Mode{Mode: mode.Developer, Repo: s.Repo}, "", "  ")
	for _, f := range []struct {
		path string
		data []byte
	}{{h.ComposeFile(), docker.Compose}, {h.OverrideFile(), override(s)}, {h.ModeFile(), append(modeJSON, '\n')}} {
		if err := home.WriteFile(f.path, f.data, 0o644); err != nil {
			return fail(err)
		}
	}

	// 4. Replaced jobs and old containers.
	o.Log("[4/5] Disabling the replaced launchd jobs and stopping the old containers")
	done["launchd"] = true
	for _, label := range replacedJobs {
		if p.LaunchAgents != "" && home.Exists(filepath.Join(p.LaunchAgents, label+".plist")) {
			o.run(ctx, "launchctl", "bootout", "gui/"+o.UID()+"/"+label)
			if err := o.check(ctx, run.Cmd{Name: "launchctl", Args: []string{"disable", "gui/" + o.UID() + "/" + label}}); err != nil {
				return fail(err)
			}
			rec.Jobs = append(rec.Jobs, label)
		}
	}
	done["containers"] = true
	stops := []string{}
	if srv := serverContainers[s.Kind]; srv != "" {
		stops = append(stops, srv)
	}
	if s.Kind == KindHost {
		o.run(ctx, "docker", "stop", proxyContainer) // optional: only Spark setups have it
		rec.Containers = append(rec.Containers, proxyContainer)
	}
	stops = append(stops, s.Falkordb)
	for _, c := range stops {
		if err := o.check(ctx, run.Cmd{Name: "docker", Args: []string{"stop", c}, Timeout: time.Minute}); err != nil {
			return fail(err)
		}
		if !contains(rec.Containers, c) {
			rec.Containers = append(rec.Containers, c)
		}
	}
	if err := writeRecord(); err != nil {
		return fail(err)
	}

	// 5. Journal, preview state, start, verify.
	o.Log("[5/5] Building, moving the journal into the " + JournalVolume + " volume, starting and verifying")
	if err := o.check(ctx, compose(h, "build", "cmf-server")); err != nil {
		return fail(err)
	}
	if s.Kind == KindPreview && s.StateVolume != "" {
		// Proposals and other state into the bind mount; the journal goes to its volume below.
		script := "for d in proposals-docs proposals-episode; do [ -e /old/$d ] && cp -a /old/$d /data/cmf/; done; " +
			"mkdir -p /data/cmf/imports; for f in /old/imports/* /old/imports/.[!.]*; do [ -e \"$f\" ] && [ \"$(basename \"$f\")\" != journal ] && cp -a \"$f\" /data/cmf/imports/; done; true"
		if err := o.check(ctx, compose(h, "run", "--rm", "--no-deps", "-T", "-v", s.StateVolume+":/old:ro", "--entrypoint", "sh", "cmf-server", "-c", script)); err != nil {
			return fail(err)
		}
	}
	events, err := o.fillJournal(ctx, h, s)
	if err != nil {
		return fail(err)
	}
	if events != "" {
		journal := filepath.Join(h.Dir, "state", "imports", "journal")
		if isDir(journal) {
			if err := os.Rename(journal, journal+".moved-"+stamp); err != nil {
				return fail(err)
			}
		}
		os.MkdirAll(filepath.Dir(journal), 0o700)
		os.WriteFile(filepath.Join(filepath.Dir(journal), journalMarker), []byte("The journal is in the Docker volume "+JournalVolume+".\n"), 0o644)
		o.Log("  journal moved: " + events + " (events, quick_check)")
	}
	done["stack"] = true
	if err := o.check(ctx, compose(h, "up", "-d")); err != nil {
		return fail(err)
	}
	if err := verify(ctx, o, s); err != nil {
		return fail(err)
	}
	o.Log(fmt.Sprintf("\nDone. CMF now runs from %s (graph %q, %d nodes). Undo with: cmf migrate restore %s", h.Dir, s.Graph, s.Nodes, stamp))
	return rec, nil
}

// verify waits for /health on the new stack and compares graph and node count.
func verify(ctx context.Context, o Ops, s *Survey) error {
	url := fmt.Sprintf("http://127.0.0.1:%d/health", s.Port)
	var h probe.Health
	ok := false
	for i := 0; i < 60; i++ { // 3 minutes
		if body, err := o.Get(ctx, url); err == nil && json.Unmarshal(body, &h) == nil && h.Status == "ok" {
			ok = true
			break
		}
		o.Sleep(3 * time.Second)
	}
	if !ok {
		return fmt.Errorf("/health on port %d didn't answer within 3 minutes", s.Port)
	}
	if h.Graph != s.Graph || !h.FalkorDB.Reachable || !h.FalkorDB.GraphExists {
		return fmt.Errorf("/health reports graph %q (FalkorDB reachable %t, graph exists %t), expected %q", h.Graph, h.FalkorDB.Reachable, h.FalkorDB.GraphExists, s.Graph)
	}
	after := *s
	after.Falkordb = "cmf-falkordb"
	if n, msg := o.nodes(ctx, &after); n != s.Nodes {
		return fmt.Errorf("the graph has %d nodes after the move (%s), %d before", n, msg, s.Nodes)
	}
	return nil
}

// undo reverses the steps in done (every step when done is nil).
func undo(ctx context.Context, p Paths, o Ops, rec *Record, done map[string]bool) {
	all := done == nil
	h := p.Home
	if all || done["stack"] {
		o.Runner.Run(ctx, compose(h, "down")) // never -v: volumes stay
	}
	if all || done["env"] {
		repoEnv := filepath.Join(rec.Repo, ".env")
		if migrated := repoEnv + ".migrated-" + rec.Stamp; home.Exists(migrated) && !home.Exists(repoEnv) {
			os.Rename(migrated, repoEnv)
		}
		if home.Exists(h.EnvFile()) {
			os.Rename(h.EnvFile(), h.EnvFile()+".rolled-back-"+rec.Stamp)
		}
	}
	if all || done["launchd"] {
		for _, label := range rec.Jobs {
			o.run(ctx, "launchctl", "enable", "gui/"+o.UID()+"/"+label)
			o.run(ctx, "launchctl", "bootstrap", "gui/"+o.UID(), filepath.Join(p.LaunchAgents, label+".plist"))
		}
	}
	if all || done["containers"] {
		for i := len(rec.Containers) - 1; i >= 0; i-- { // FalkorDB first: the server depends on it
			o.run(ctx, "docker", "start", rec.Containers[i])
		}
	}
	o.Log("Restored: the repo .env, launchd jobs and old containers. Backups, ~/.cmf/state and the " + JournalVolume + " volume are kept.")
}

// Restore undoes a migration from its backup record.
func Restore(ctx context.Context, p Paths, o Ops, stamp string) error {
	data, err := os.ReadFile(filepath.Join(p.Home.BackupsDir(), stamp, MarkerFile))
	if err != nil {
		return fmt.Errorf("no cmf migrate backup %s: %w", stamp, err)
	}
	var rec Record
	if err := json.Unmarshal(data, &rec); err != nil {
		return err
	}
	undo(ctx, p, o, &rec, nil)
	return nil
}
