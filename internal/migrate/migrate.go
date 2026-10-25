// Package migrate moves a repo-based CMF install into ~/.cmf (docs/
// setup-simplification-plan.md, items 2.5 and 3.5). It is the Go form of
// scripts/consolidate_install.py, widened from Todd's host install to the
// repo's preview stack, and needs no Python on the machine.
//
// Two kinds of old install are recognised:
//
//   - host: docker-compose.yml's FalkorDB (context-memory-fabric-falkordb)
//     with the server and pollers run by launchd from the repo, state in the
//     repo (or CMF_STATE_DIR);
//   - preview: docker-compose.preview.yml's cmf-preview-falkordb and
//     cmf-preview-server, state and journal in a named volume.
//
// The sequence: back up everything first (repo .env, state, a FalkorDB dump,
// launchd plists), copy config and state into ~/.cmf, write the compose
// files with the old FalkorDB volume attached as is (no data copy), disable
// (never uninstall) the replaced launchd jobs and stop (never remove) the
// old containers, move the journal into its volume, start the stack and
// check the same graph answers with the same node count. Any failure
// restores step by step; `cmf migrate restore <stamp>` undoes a migration.
package migrate

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/home"
	"github.com/tmargolis/context-memory-fabric/internal/probe"
	"github.com/tmargolis/context-memory-fabric/internal/run"
)

const (
	KindHost    = "host"
	KindPreview = "preview"

	JournalVolume = "cmf-journal"
	journalMarker = "journal.in-docker-volume" // server.core.env.JOURNAL_VOLUME_MARKER
	inVolume      = "/data/cmf/imports/journal"
	rulesKey      = "CMF_REVIEW_RULES_PATH"
	rulesFile     = "review-rules.md"
	// MarkerFile marks a backup made by `cmf migrate` (cmf restore points at
	// `cmf migrate restore` for it).
	MarkerFile = "migrate.json"
)

// Old containers, by kind.
var (
	falkordbContainers = map[string]string{KindHost: "context-memory-fabric-falkordb", KindPreview: "cmf-preview-falkordb"}
	serverContainers   = map[string]string{KindPreview: "cmf-preview-server"}
	proxyContainer     = "context-memory-fabric-spark-llm-proxy"
	replacedJobs       = []string{"com.cmf.mcp-server", "com.cmf.capture-poller"}
	stateItems         = []string{"proposals-docs", "proposals-episode", "imports"}
	// Where each capture source's transcripts live on macOS, for the poller's mounts.
	captureDirs = map[string]string{
		"CMF_CAPTURE_CLAUDE_CODE_DIR": ".claude/projects",
		"CMF_CAPTURE_CODEX_DIR":       ".codex/sessions",
		"CMF_CAPTURE_ANTIGRAVITY_DIR": ".gemini/antigravity",
		"CMF_CAPTURE_COWORK_DIR":      "Library/Application Support/Claude/local-agent-mode-sessions",
	}
	captureOrder = []string{"CMF_CAPTURE_CLAUDE_CODE_DIR", "CMF_CAPTURE_CODEX_DIR", "CMF_CAPTURE_ANTIGRAVITY_DIR", "CMF_CAPTURE_COWORK_DIR"}
	// Path settings compose maps into the containers itself, or that are
	// patterns rather than files to open.
	mappedPathKeys = map[string]bool{"LLM_WIKI_PATH": true, "CMF_STATE_DIR": true, "CMF_REPO": true,
		"CMF_PROJECT_ROOTS": true, "CMF_PROJECT_FOLDER_MAP": true}
)

// Ops is every side effect, injectable for tests.
type Ops struct {
	Runner run.Runner
	Get    probe.HealthGetter
	Sleep  func(time.Duration)
	Now    func() time.Time
	Log    func(string)
	UID    func() string
}

// Paths locates the machine's folders.
type Paths struct {
	Repo         string // "" to find it from the old containers or launchd
	UserHome     string
	Home         home.Home // ~/.cmf
	LaunchAgents string    // "" off macOS
}

type mount struct {
	Type, Name, Source, Destination string
}

// Survey is what the plan and its preconditions read. Read-only.
type Survey struct {
	Kind        string
	Repo        string
	RepoEnv     *home.Env
	Graph       string
	Port        int
	StateRoot   string   // host kind: where proposals-docs etc. live
	StateItems  []string // host kind: which of them exist
	StateVolume string   // preview kind: the old server's /data/cmf volume
	Falkordb    string   // old FalkorDB container
	DataVolume  string   // its data volume
	DataDir     string   // where it writes (CONFIG GET dir)
	Running     bool
	Nodes       int // -1 unknown
	NodesErr    string
	Plists      []string
	CaptureDirs map[string]string
	HostPaths   map[string]string
	Rules       string // a CMF_REVIEW_RULES_PATH file to move into state/
	DockerOK    bool
	Blockers    []string

	dataOutside string // a volume is mounted, but FalkorDB writes elsewhere
}

func (o Ops) run(ctx context.Context, name string, args ...string) run.Result {
	return o.Runner.Run(ctx, run.Cmd{Name: name, Args: args})
}

func (o Ops) inspect(ctx context.Context, container, format string) (string, bool) {
	r := o.run(ctx, "docker", "inspect", "-f", format, container)
	return strings.TrimSpace(r.Stdout), r.Err == nil
}

func (o Ops) mounts(ctx context.Context, container string) []mount {
	out, ok := o.inspect(ctx, container, "{{json .Mounts}}")
	var ms []mount
	if ok {
		json.Unmarshal([]byte(out), &ms)
	}
	return ms
}

var plistDir = regexp.MustCompile(`<key>WorkingDirectory</key>\s*<string>([^<]+)</string>`)

// redis runs redis-cli in the old FalkorDB container with the repo's credentials.
func (o Ops) redis(ctx context.Context, s *Survey, args ...string) run.Result {
	cmd := run.Cmd{Name: "docker", Args: []string{"exec"}}
	if pw := s.RepoEnv.Get("FALKORDB_PASSWORD"); pw != "" {
		cmd.Args = append(cmd.Args, "-e", "REDISCLI_AUTH")
		cmd.Env = []string{"REDISCLI_AUTH=" + pw}
	}
	cmd.Args = append(cmd.Args, s.Falkordb, "redis-cli", "--no-auth-warning")
	if u := s.RepoEnv.Get("FALKORDB_USER"); u != "" {
		cmd.Args = append(cmd.Args, "--user", u)
	}
	cmd.Args = append(cmd.Args, args...)
	return o.Runner.Run(ctx, cmd)
}

func (o Ops) nodes(ctx context.Context, s *Survey) (int, string) {
	r := o.redis(ctx, s, "GRAPH.RO_QUERY", s.Graph, "MATCH (n) RETURN count(n)")
	lines := strings.Split(strings.TrimSpace(r.Stdout), "\n")
	if r.Err == nil && len(lines) >= 2 {
		if n, err := strconv.Atoi(strings.TrimSpace(lines[1])); err == nil {
			return n, ""
		}
	}
	return -1, strings.TrimSpace(r.Output() + " " + errText(r.Err))
}

func errText(err error) string {
	if err == nil {
		return ""
	}
	return err.Error()
}

// Look reads the machine. It never changes anything.
func Look(ctx context.Context, p Paths, o Ops) *Survey {
	s := &Survey{Nodes: -1, Port: 8000, RepoEnv: &home.Env{}, CaptureDirs: map[string]string{}, HostPaths: map[string]string{}}
	s.DockerOK = o.run(ctx, "docker", "info").Err == nil

	// Which old FalkorDB, and so which kind.
	if s.DockerOK {
		for _, kind := range []string{KindPreview, KindHost} {
			name := falkordbContainers[kind]
			if _, ok := o.inspect(ctx, name, "{{.Id}}"); ok {
				s.Kind, s.Falkordb = kind, name
				break
			}
		}
	}
	if s.Kind == "" && p.LaunchAgents != "" && home.Exists(filepath.Join(p.LaunchAgents, "com.cmf.mcp-server.plist")) {
		s.Kind, s.Falkordb = KindHost, falkordbContainers[KindHost]
	}

	// The repo: given, else the old stack's compose folder, else launchd's.
	s.Repo = p.Repo
	if s.Repo == "" && s.Falkordb != "" {
		s.Repo, _ = o.inspect(ctx, s.Falkordb, `{{index .Config.Labels "com.docker.compose.project.working_dir"}}`)
	}
	if s.Repo == "" && p.LaunchAgents != "" {
		if data, err := os.ReadFile(filepath.Join(p.LaunchAgents, "com.cmf.mcp-server.plist")); err == nil {
			if m := plistDir.FindSubmatch(data); m != nil {
				s.Repo = strings.TrimSpace(string(m[1]))
			}
		}
	}
	if s.Repo != "" {
		if env, err := home.ReadEnv(filepath.Join(s.Repo, ".env")); err == nil {
			s.RepoEnv = env
		}
	}
	env := s.RepoEnv
	s.Graph = env.Get("FALKORDB_DATABASE")
	if s.Graph == "" {
		s.Graph = map[string]string{KindPreview: "CMF-local", KindHost: "default_db"}[s.Kind]
	}
	if port, err := strconv.Atoi(env.Get("CMF_MCP_PORT")); err == nil && port > 0 {
		s.Port = port
	}

	if s.Falkordb != "" && s.DockerOK {
		running, _ := o.inspect(ctx, s.Falkordb, "{{.State.Running}}")
		s.Running = running == "true"
		if s.Running {
			if r := o.redis(ctx, s, "CONFIG", "GET", "dir"); r.Err == nil {
				if lines := strings.Split(strings.TrimSpace(r.Stdout), "\n"); len(lines) >= 2 {
					s.DataDir = strings.TrimSpace(lines[1])
				}
			}
			s.Nodes, s.NodesErr = o.nodes(ctx, s)
		}
		// The data volume: the volume mount that holds the folder FalkorDB
		// writes to. If none does, the graph lives inside the container.
		for _, m := range o.mounts(ctx, s.Falkordb) {
			if m.Type != "volume" {
				continue
			}
			if s.DataDir == "" || s.DataDir == m.Destination || strings.HasPrefix(s.DataDir, strings.TrimSuffix(m.Destination, "/")+"/") {
				s.DataVolume = m.Name
			} else if s.DataVolume == "" {
				s.dataOutside = m.Name
			}
		}
	}
	if srv := serverContainers[s.Kind]; srv != "" && s.DockerOK {
		for _, m := range o.mounts(ctx, srv) {
			if m.Type == "volume" && m.Destination == "/data/cmf" {
				s.StateVolume = m.Name
			}
		}
	}

	if s.Kind == KindHost {
		s.StateRoot = s.Repo
		if d := env.Get("CMF_STATE_DIR"); d != "" {
			s.StateRoot = expand(d, p.UserHome)
		}
		for _, item := range stateItems {
			if s.StateRoot != "" && home.Exists(filepath.Join(s.StateRoot, item)) {
				s.StateItems = append(s.StateItems, item)
			}
		}
	}
	if p.LaunchAgents != "" {
		matches, _ := filepath.Glob(filepath.Join(p.LaunchAgents, "com.cmf.*.plist"))
		s.Plists = matches
	}
	for _, k := range captureOrder {
		if dir := filepath.Join(p.UserHome, captureDirs[k]); isDir(dir) {
			s.CaptureDirs[k] = dir
		}
	}
	if r := env.Get(rulesKey); r != "" {
		if path := expand(r, p.UserHome); filepath.IsAbs(path) && isFile(path) {
			s.Rules = path
		}
	}
	for _, k := range env.Keys() {
		v := env.Get(k)
		if mappedPathKeys[k] || strings.HasPrefix(k, "CMF_CAPTURE_") && strings.HasSuffix(k, "_DIR") || (k == rulesKey && s.Rules != "") {
			continue
		}
		if (strings.HasPrefix(v, "/") || strings.HasPrefix(v, "~")) && !strings.Contains(v, "://") {
			s.HostPaths[k] = v
		}
	}
	s.Blockers = blockers(p, s)
	return s
}

// Migratable: there is an old install with a .env and no ~/.cmf install yet,
// so a plan is worth showing even if Docker must be started first.
func (s *Survey) Migratable(p Paths) bool {
	return s.Kind != "" && s.Repo != "" && isFile(filepath.Join(s.Repo, ".env")) && !home.Exists(p.Home.EnvFile())
}

func blockers(p Paths, s *Survey) []string {
	var out []string
	if home.Exists(p.Home.EnvFile()) {
		out = append(out, p.Home.EnvFile()+" already exists: this machine looks migrated already (`cmf status` shows the install).")
	}
	switch {
	case s.Kind == "":
		return append(out, "No repo-based CMF install found (no context-memory-fabric-falkordb / cmf-preview-falkordb container, no com.cmf.mcp-server launchd job).")
	case s.Repo == "":
		out = append(out, "Couldn't find the repo the old install runs from; pass --repo /path/to/context-memory-fabric.")
	case !isFile(filepath.Join(s.Repo, ".env")):
		out = append(out, "No "+filepath.Join(s.Repo, ".env")+" to migrate.")
	case !isFile(filepath.Join(s.Repo, "Dockerfile")) || !isFile(filepath.Join(s.Repo, "server", "mcp.py")):
		out = append(out, s.Repo+" isn't a CMF checkout the stack can build from (no Dockerfile or server/mcp.py).")
	}
	switch {
	case !s.DockerOK:
		out = append(out, "Docker isn't answering. Start Docker Desktop first (`cmf doctor --fix=docker`).")
	case s.DataVolume == "" && s.dataOutside != "":
		out = append(out, fmt.Sprintf("%s writes its data to %s inside the container, not into volume %s, so attaching the volume would start on an empty graph. Export the graph first (see docs/SETUP.md, FalkorDB persistence).", s.Falkordb, s.DataDir, s.dataOutside))
	case s.DataVolume == "":
		out = append(out, "Couldn't find the FalkorDB data volume of "+s.Falkordb+".")
	case !s.Running:
		out = append(out, s.Falkordb+" isn't running; it's needed for the backup dump and the node count. Start it with `docker start "+s.Falkordb+"`.")
	case s.Nodes < 0:
		out = append(out, fmt.Sprintf("Couldn't count the nodes in graph %q (%s); check FALKORDB_USER / FALKORDB_PASSWORD in the repo .env.", s.Graph, s.NodesErr))
	}
	return out
}

// Describe is the plan, one line per step, for review before Apply.
func Describe(p Paths, s *Survey, stamp string) []string {
	backup := filepath.Join(p.Home.BackupsDir(), stamp)
	lines := []string{
		fmt.Sprintf("Migration plan (%s install from %s), stamp %s", s.Kind, s.Repo, stamp),
		"",
		"1. Back up into " + backup + ":",
		"   - " + filepath.Join(s.Repo, ".env"),
	}
	for _, item := range s.StateItems {
		lines = append(lines, "   - "+filepath.Join(s.StateRoot, item))
	}
	if s.Rules != "" {
		lines = append(lines, "   - "+s.Rules)
	}
	lines = append(lines, fmt.Sprintf("   - FalkorDB dump (BGSAVE, then the RDB and ACL file copied out of %s)", s.Falkordb))
	for _, pl := range s.Plists {
		lines = append(lines, "   - "+pl)
	}
	lines = append(lines,
		"2. Write "+p.Home.EnvFile()+" from the repo .env; rename the repo .env to .env.migrated-"+stamp+" (the original values stay in the backup).",
		"   Added: CMF_REPO="+s.Repo+", COMPOSE_PROFILES with capture"+captureList(s))
	switch s.Kind {
	case KindHost:
		if len(s.StateItems) > 0 {
			lines = append(lines, "   Copy "+strings.Join(s.StateItems, ", ")+" into "+filepath.Join(p.Home.Dir, "state")+".")
		}
	case KindPreview:
		lines = append(lines, "   Copy the proposals and imports from volume "+s.StateVolume+" into "+filepath.Join(p.Home.Dir, "state")+".")
	}
	if s.Rules != "" {
		lines = append(lines, "   Copy "+s.Rules+" to state/"+rulesFile+" and comment out "+rulesKey+" (the original stays).")
	}
	lines = append(lines,
		"3. Write compose.yml and compose.override.yml (Developer mode: builds from "+s.Repo+"); FalkorDB volume "+s.DataVolume+" attached as is, no data copy.")
	// The order Apply stops them in: the server before the FalkorDB it uses.
	var stops []string
	if srv := serverContainers[s.Kind]; srv != "" {
		stops = append(stops, srv)
	}
	if s.Kind == KindHost {
		stops = append(stops, proxyContainer+" (if present)")
	}
	stops = append(stops, s.Falkordb)
	if len(s.Plists) > 0 {
		lines = append(lines, "4. Disable (bootout + disable; plists stay): "+strings.Join(replacedJobs, ", ")+". Other com.cmf.* jobs are left alone.")
	} else {
		lines = append(lines, "4. (No launchd jobs to disable.)")
	}
	lines = append(lines, "   Stop (not remove): "+strings.Join(stops, ", ")+".",
		"5. Move the journal into the "+JournalVolume+" volume (checked: same event count, quick_check ok), `docker compose up -d --build`,",
		fmt.Sprintf("   then check /health on port %d answers for graph %q with %d nodes, as now. Any failure restores the previous setup.", s.Port, s.Graph, s.Nodes))
	if len(s.HostPaths) > 0 {
		lines = append(lines, "", "Settings naming a host path the containers can't see (host-side tools still use them):")
		for _, k := range sortedKeys(s.HostPaths) {
			lines = append(lines, "   - "+k+"="+s.HostPaths[k])
		}
	}
	return lines
}

func captureList(s *Survey) string {
	var parts []string
	for _, k := range captureOrder {
		if v, ok := s.CaptureDirs[k]; ok {
			parts = append(parts, k+"="+v)
		}
	}
	if len(parts) == 0 {
		return ""
	}
	return ", " + strings.Join(parts, ", ")
}

func expand(path, userHome string) string {
	if strings.HasPrefix(path, "~/") {
		return filepath.Join(userHome, path[2:])
	}
	return path
}

func isDir(p string) bool  { i, err := os.Stat(p); return err == nil && i.IsDir() }
func isFile(p string) bool { i, err := os.Stat(p); return err == nil && i.Mode().IsRegular() }

func sortedKeys(m map[string]string) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

var errBlocked = errors.New("blocked")
