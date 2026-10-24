// Package probe reads the machine's CMF state without changing anything:
// Docker, the ~/.cmf stack, leftovers from older installs, the port, and the
// server's /health. The source of truth is the machine (plan section 3.5);
// cmf keeps no state of its own that could disagree with it.
package probe

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/home"
	"github.com/tmargolis/context-memory-fabric/internal/mode"
	"github.com/tmargolis/context-memory-fabric/internal/run"
)

// Project is the compose project name of the ~/.cmf stack.
const Project = "cmf"

type Docker struct {
	Installed  bool
	Responding bool
	Hung       bool // the CLI is there but the daemon didn't answer in time
	Version    string
	Error      string
}

type Container struct {
	Name    string
	Service string
	Project string
	State   string // running, exited, ...
	Health  string // healthy, unhealthy, starting, or ""
}

type Listener struct {
	Command string
	PID     int
}

type LaunchdJob struct {
	Label    string
	Loaded   bool
	PID      int
	Disabled bool
}

type Process struct {
	PID     int
	Command string
}

type Health struct {
	Status    string `json:"status"`
	Version   string `json:"version"`
	Runtime   string `json:"runtime"`
	StartedAt string `json:"started_at"`
	Graph     string `json:"graph"`
	FalkorDB  struct {
		Address     string `json:"address"`
		Reachable   bool   `json:"reachable"`
		GraphExists bool   `json:"graph_exists"`
		Error       string `json:"error"`
	} `json:"falkordb"`
	Providers struct {
		LLM          string `json:"llm"`
		Embeddings   string `json:"embeddings"`
		CaptureLLM   string `json:"capture_llm"`
		EmbeddingDim int    `json:"embedding_dim"`
	} `json:"providers"`
	KnowledgeSources []string          `json:"knowledge_sources"`
	BackgroundWrites []BackgroundWrite `json:"background_writes"`
	ClientsSeen      []ClientSeen      `json:"clients_seen"`
}

type BackgroundWrite struct {
	Status string `json:"status"`
	Reason string `json:"reason"`
	At     string `json:"at"`
}

type ClientSeen struct {
	Harness  string    `json:"harness"`
	LastSeen time.Time `json:"last_seen"`
	Calls    int       `json:"calls"`
}

type Facts struct {
	At        time.Time
	OS        string
	Home      string
	Mode      mode.State
	ModeErr   string
	Env       map[string]string // the few .env values checks need
	Port      int
	Docker    Docker
	Stack     []Container // containers of the cmf project
	Strays    []Container // other CMF-looking containers (older installs)
	Listener  *Listener   // who listens on Port, if anyone
	Launchd   []LaunchdJob
	HostMCP   []Process // server.mcp running directly on the host
	Health    *Health
	HealthErr string
	Nodes     int // -1 when unknown
	NodesErr  string
}

// Service returns the stack's container for a compose service, if any.
func (f Facts) Service(name string) *Container {
	for i := range f.Stack {
		if f.Stack[i].Service == name {
			return &f.Stack[i]
		}
	}
	return nil
}

// Profiles is COMPOSE_PROFILES from .env.
func (f Facts) Profiles() []string {
	var out []string
	for _, p := range strings.Split(f.Env["COMPOSE_PROFILES"], ",") {
		if p = strings.TrimSpace(p); p != "" {
			out = append(out, p)
		}
	}
	return out
}

func (f Facts) HasProfile(name string) bool {
	for _, p := range f.Profiles() {
		if p == name {
			return true
		}
	}
	return false
}

// HealthGetter fetches /health; replaced in tests.
type HealthGetter func(ctx context.Context, url string) ([]byte, error)

func HTTPGet(ctx context.Context, url string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if err != nil {
		return nil, err
	}
	if resp.StatusCode != http.StatusOK {
		return body, fmt.Errorf("HTTP %d", resp.StatusCode)
	}
	return body, nil
}

type Prober struct {
	Home   home.Home
	Runner run.Runner
	Get    HealthGetter
	OS     string // runtime.GOOS, injectable
	Now    func() time.Time
}

// envKeys are the .env values checks read. FALKORDB_PASSWORD is used for the
// node count and never leaves the probe.
var envKeys = []string{"COMPOSE_PROFILES", "CMF_MCP_PORT", "FALKORDB_USER", "FALKORDB_PASSWORD", "CMF_REPO"}

func (p Prober) Collect(ctx context.Context) Facts {
	f := Facts{At: p.Now(), OS: p.OS, Home: p.Home.Dir, Env: map[string]string{}, Port: 8000, Nodes: -1}
	if env, err := home.ReadEnv(p.Home.EnvFile()); err == nil {
		for _, k := range envKeys {
			if v := env.Get(k); v != "" {
				f.Env[k] = v
			}
		}
	}
	if port, err := strconv.Atoi(f.Env["CMF_MCP_PORT"]); err == nil && port > 0 {
		f.Port = port
	}
	if state, err := mode.Load(p.Home); err != nil {
		f.ModeErr = err.Error()
	} else {
		f.Mode = state
	}

	f.Docker = p.docker(ctx)
	if f.Docker.Responding {
		f.Stack = p.stack(ctx)
		f.Strays = p.strays(ctx)
	}
	f.Listener = p.listener(ctx, f.Port)
	if p.OS == "darwin" {
		f.Launchd = p.launchd(ctx)
	}
	if p.OS != "linux" {
		// On Linux, container processes show up in the host's process list.
		f.HostMCP = p.hostMCP(ctx)
	}
	p.health(ctx, &f)
	if f.Docker.Responding && f.Health != nil && f.Health.Graph != "" {
		f.Nodes, f.NodesErr = p.nodes(ctx, f)
	}
	f.Env["FALKORDB_PASSWORD"] = ""
	delete(f.Env, "FALKORDB_PASSWORD")
	return f
}

func (p Prober) docker(ctx context.Context) Docker {
	r := p.Runner.Run(ctx, run.Cmd{Name: "docker", Args: []string{"version", "--format", "{{.Server.Version}}"}, Timeout: 15 * time.Second})
	switch {
	case errors.Is(r.Err, run.ErrNotFound):
		return Docker{Error: "the docker command isn't installed"}
	case errors.Is(r.Err, run.ErrTimeout):
		return Docker{Installed: true, Hung: true, Error: "Docker didn't answer within 15 seconds"}
	case r.Err != nil:
		return Docker{Installed: true, Error: firstLine(r.Output())}
	}
	return Docker{Installed: true, Responding: true, Version: strings.TrimSpace(r.Stdout)}
}

func (p Prober) compose(args ...string) run.Cmd {
	return run.Cmd{Name: "docker", Args: append([]string{"compose", "--project-directory", p.Home.Dir}, args...)}
}

// psRow is one line of `docker compose ps --format json` / `docker ps --format json`.
type psRow struct {
	Name    string
	Names   string
	Service string
	Project string
	State   string
	Health  string
	Labels  string
}

// parseRows reads either NDJSON or a JSON array (compose versions differ).
func parseRows(out string) []psRow {
	out = strings.TrimSpace(out)
	var rows []psRow
	if strings.HasPrefix(out, "[") {
		json.Unmarshal([]byte(out), &rows)
		return rows
	}
	scanner := bufio.NewScanner(strings.NewReader(out))
	scanner.Buffer(make([]byte, 64*1024), 4*1024*1024)
	for scanner.Scan() {
		var row psRow
		if json.Unmarshal(scanner.Bytes(), &row) == nil {
			rows = append(rows, row)
		}
	}
	return rows
}

func (p Prober) stack(ctx context.Context) []Container {
	r := p.Runner.Run(ctx, p.compose("ps", "--all", "--format", "json"))
	var out []Container
	for _, row := range parseRows(r.Stdout) {
		out = append(out, Container{Name: first(row.Name, row.Names), Service: row.Service, Project: first(row.Project, Project), State: row.State, Health: row.Health})
	}
	return out
}

func label(labels, key string) string {
	for _, kv := range strings.Split(labels, ",") {
		if k, v, ok := strings.Cut(kv, "="); ok && k == key {
			return v
		}
	}
	return ""
}

// strays are CMF containers that aren't part of the ~/.cmf stack: the
// repo-based preview or dev stacks from before consolidation.
func (p Prober) strays(ctx context.Context) []Container {
	r := p.Runner.Run(ctx, run.Cmd{Name: "docker", Args: []string{"ps", "--all", "--format", "json"}})
	var out []Container
	for _, row := range parseRows(r.Stdout) {
		name := first(row.Names, row.Name)
		project := label(row.Labels, "com.docker.compose.project")
		lower := strings.ToLower(name + " " + project)
		if project == Project || !(strings.Contains(lower, "cmf") || strings.Contains(lower, "context-memory-fabric")) {
			continue
		}
		out = append(out, Container{Name: name, Project: project, State: row.State, Service: label(row.Labels, "com.docker.compose.service")})
	}
	return out
}

func (p Prober) listener(ctx context.Context, port int) *Listener {
	if p.OS == "windows" {
		return nil // netstat parsing comes with the Windows test pass (plan section 5).
	}
	r := p.Runner.Run(ctx, run.Cmd{Name: "lsof", Args: []string{"-nP", "-iTCP:" + strconv.Itoa(port), "-sTCP:LISTEN", "-Fpc"}})
	// -F output: p<pid> then c<command> lines.
	var l Listener
	for _, line := range strings.Split(r.Stdout, "\n") {
		switch {
		case strings.HasPrefix(line, "p") && l.PID == 0:
			l.PID, _ = strconv.Atoi(line[1:])
		case strings.HasPrefix(line, "c") && l.Command == "":
			l.Command = line[1:]
		}
	}
	if l.PID == 0 {
		return nil
	}
	return &l
}

// IsDocker reports whether the listener is Docker's port forwarder.
func (l Listener) IsDocker() bool {
	c := strings.ToLower(l.Command)
	return strings.HasPrefix(c, "com.docke") || strings.Contains(c, "docker") || strings.Contains(c, "vpnkit") || strings.Contains(c, "rootlesskit")
}

func (p Prober) launchd(ctx context.Context) []LaunchdJob {
	jobs := map[string]*LaunchdJob{}
	var order []string
	get := func(label string) *LaunchdJob {
		if jobs[label] == nil {
			jobs[label] = &LaunchdJob{Label: label}
			order = append(order, label)
		}
		return jobs[label]
	}
	list := p.Runner.Run(ctx, run.Cmd{Name: "launchctl", Args: []string{"list"}})
	for _, line := range strings.Split(list.Stdout, "\n") {
		fields := strings.Fields(line)
		if len(fields) == 3 && strings.HasPrefix(fields[2], "com.cmf.") {
			job := get(fields[2])
			job.Loaded = true
			job.PID, _ = strconv.Atoi(fields[0])
		}
	}
	uid := p.Runner.Run(ctx, run.Cmd{Name: "id", Args: []string{"-u"}})
	disabled := p.Runner.Run(ctx, run.Cmd{Name: "launchctl", Args: []string{"print-disabled", "gui/" + strings.TrimSpace(uid.Stdout)}})
	for _, line := range strings.Split(disabled.Stdout, "\n") {
		label, state, ok := strings.Cut(strings.TrimSpace(line), "=>")
		label = strings.Trim(strings.TrimSpace(label), `"`)
		if ok && strings.HasPrefix(label, "com.cmf.") {
			get(label).Disabled = strings.TrimSpace(state) == "disabled"
		}
	}
	out := make([]LaunchdJob, 0, len(order))
	for _, label := range order {
		out = append(out, *jobs[label])
	}
	return out
}

func (p Prober) hostMCP(ctx context.Context) []Process {
	r := p.Runner.Run(ctx, run.Cmd{Name: "pgrep", Args: []string{"-fl", "server\\.mcp"}})
	var out []Process
	for _, line := range strings.Split(strings.TrimSpace(r.Stdout), "\n") {
		pid, cmd, ok := strings.Cut(strings.TrimSpace(line), " ")
		n, err := strconv.Atoi(pid)
		if !ok || err != nil || strings.Contains(cmd, "pgrep") {
			continue
		}
		out = append(out, Process{PID: n, Command: cmd})
	}
	return out
}

func (p Prober) health(ctx context.Context, f *Facts) {
	body, err := p.Get(ctx, fmt.Sprintf("http://127.0.0.1:%d/health", f.Port))
	if err != nil {
		f.HealthErr = err.Error()
		return
	}
	var h Health
	if err := json.Unmarshal(body, &h); err != nil {
		f.HealthErr = "the answer on /health isn't CMF's: " + err.Error()
		return
	}
	f.Health = &h
}

// nodes counts the graph's nodes through redis-cli inside the FalkorDB
// container; the password goes through the environment, not the argv.
func (p Prober) nodes(ctx context.Context, f Facts) (int, string) {
	db := f.Service("falkordb")
	if db == nil || db.State != "running" {
		return -1, "FalkorDB isn't running"
	}
	args := []string{"exec"}
	var env []string
	if pw := f.Env["FALKORDB_PASSWORD"]; pw != "" {
		args = append(args, "-e", "REDISCLI_AUTH")
		env = []string{"REDISCLI_AUTH=" + pw}
	}
	args = append(args, db.Name, "redis-cli", "--no-auth-warning")
	if user := f.Env["FALKORDB_USER"]; user != "" {
		args = append(args, "--user", user)
	}
	args = append(args, "GRAPH.QUERY", f.Health.Graph, "MATCH (n) RETURN count(n)")
	r := p.Runner.Run(ctx, run.Cmd{Name: "docker", Args: args, Env: env})
	lines := strings.Split(strings.TrimSpace(r.Stdout), "\n")
	if r.Err == nil && len(lines) >= 2 {
		if n, err := strconv.Atoi(strings.TrimSpace(lines[1])); err == nil {
			return n, ""
		}
	}
	return -1, firstLine(r.Output())
}

func first(values ...string) string {
	for _, v := range values {
		if v != "" {
			return v
		}
	}
	return ""
}

func firstLine(s string) string {
	line, _, _ := strings.Cut(strings.TrimSpace(s), "\n")
	return line
}
