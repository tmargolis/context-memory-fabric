// Package checks turns probe.Facts into the Status rows of plan section 3.3:
// each green / yellow / red with a one-line explanation and, where cmf can
// do it, a Fix. Rows the server can't answer yet (an older server) show as
// "n/a".
package checks

import (
	"context"
	"fmt"
	"os"
	"slices"
	"strings"
	"time"

	contextmemoryfabric "github.com/tmargolis/context-memory-fabric"
	"github.com/tmargolis/context-memory-fabric/internal/capture"
	"github.com/tmargolis/context-memory-fabric/internal/clients"
	"github.com/tmargolis/context-memory-fabric/internal/mode"
	"github.com/tmargolis/context-memory-fabric/internal/probe"
	"github.com/tmargolis/context-memory-fabric/internal/run"
)

type Level string

const (
	OK   Level = "ok"
	Warn Level = "warn"
	Fail Level = "fail"
	NA   Level = "n/a"
)

type Step struct {
	Note string  `json:"note"`
	Cmd  run.Cmd `json:"-"`
	Show string  `json:"command"`
}

type Fix struct {
	Label string `json:"label"`
	Steps []Step `json:"steps"`
}

type Result struct {
	ID     string `json:"id"`
	Title  string `json:"title"`
	About  string `json:"about,omitempty"` // what the row checks and the options, in plain words
	Level  Level  `json:"level"`
	Detail string `json:"detail"`
	Fix    *Fix   `json:"fix,omitempty"`
	// Action can be run whatever the row's state (the self-test's "run it
	// now"); Fix is offered only when something is wrong.
	Action *Fix `json:"action,omitempty"`
	// Where the row's setting is changed: a feature id or "group:<id>" on the
	// Features page, or Doc for things set outside the app.
	Feature string `json:"feature,omitempty"`
	Doc     string `json:"doc,omitempty"`
}

const docs = "https://github.com/tmargolis/context-memory-fabric/blob/main/docs/"

// guide is each row's description and where it is changed. Features follow
// the manifest's "checks" lists.
var guide = map[string]struct{ About, Feature, Doc string }{
	"mode": {About: "Standard runs the published CMF server image; Developer builds it from a source checkout. Either way CMF keeps its settings, compose file and backups in one home folder. Changed with `cmf mode`, not here.",
		Doc: docs + "SETUP.md#install-modes-and-the-cmf-home"},
	"docker": {About: "CMF runs in Docker, so Docker Desktop (or OrbStack or Colima on a Mac) has to be running.",
		Doc: docs + "QUICKSTART.md#1-install-docker-desktop"},
	"stack": {About: "The containers CMF needs: the memory server, its FalkorDB graph database, and the capture poller when capture is on.",
		Feature: "core"},
	"port": {About: "AI apps reach CMF on this computer's port 8000, so only one CMF server can listen there.",
		Feature: "core"},
	"launchd": {About: "Older installs ran CMF as macOS background jobs. The stack does their work now, so they should be off; jobs CMF didn't create are only listed.",
		Doc: docs + "SETUP.md#install-modes-and-the-cmf-home"},
	"containers": {About: "Containers from an older CMF install (the Docker preview) shouldn't run beside this one. Stopped ones keep their data; `cmf migrate` moves it over.",
		Doc: docs + "SETUP.md#install-modes-and-the-cmf-home"},
	"host-server": {About: "A CMF server started by hand outside Docker would compete with the stack for port 8000.",
		Doc: docs + "SETUP.md#install-modes-and-the-cmf-home"},
	"graph": {About: "Where your memories are stored. A new install has none until the first memory is written.",
		Feature: "core"},
	"providers": {About: "The models CMF calls: an LLM that extracts memories (backups take over on rate limits), an embedding model for search, and the model background capture uses.",
		Feature: "llm"},
	"embedding": {About: "Every vector in a graph has to be the same width, so the embedding model can't change once the graph has memories.",
		Feature: "embeddings"},
	"knowledge": {About: "Notes CMF searches alongside your memories: a Markdown folder, Notion, the demo wiki, or none.",
		Feature: "group:knowledge"},
	"capture": {About: "The poller reads your AI apps' transcripts in the background and queues what's worth keeping for review.",
		Feature: "capture"},
	"clients": {About: "Each AI app needs one CMF entry in its own config. Duplicates or a stale address make it connect twice or not at all. Apps that use a claude.ai connector don't need one.",
		Feature: "group:apps"},
	"connector": {About: "One connector on your claude.ai account reaches CMF from Claude Desktop (Chat, Cowork and the Code tab), Claude Code and claude.ai, with nothing to set up in each app. It needs remote access on.",
		Feature: "client:connector"},
	"app-claude-code": {About: "Claude Code in a terminal and the Desktop app's Code tab. Its entry lives in `~/.claude.json`; both write transcripts to the same folder. Calls through a claude.ai connector show under Claude Desktop.",
		Feature: "client:claude-code"},
	"app-claude-desktop": {About: "Chat and Cowork share Claude Desktop's connection: a claude.ai connector, or its config file (`cmf bridge`). Chat keeps no transcripts on disk; the poller reads Cowork's local sessions.",
		Feature: "client:claude-desktop"},
	"app-codex": {About: "OpenAI's Codex CLI. Its entry lives in `~/.codex/config.toml`; its checkpoint hook in `~/.codex/hooks.json`.",
		Feature: "client:codex"},
	"app-antigravity": {About: "Google's Antigravity. Its entry and checkpoint hook live under `~/.gemini`.",
		Feature: "client:antigravity"},
	"app-cursor": {About: "Cursor connects through its MCP settings. It has no hooks and keeps no transcripts CMF can read.",
		Feature: "client:cursor"},
	"app-vscode": {About: "VS Code's MCP support (Copilot agent mode). No hooks or transcripts CMF can read.",
		Feature: "client:vscode"},
	"selftest": {About: "Writes a test memory to a scratch graph, finds it again and deletes the graph. Your own memories aren't touched."},
}

// tilde shortens a path under the user's home to ~/...
func tilde(path string) string {
	if h, err := os.UserHomeDir(); err == nil && h != "" && (path == h || strings.HasPrefix(path, h+"/")) {
		return "~" + strings.TrimPrefix(path, h)
	}
	return path
}

func step(note string, cmd run.Cmd) Step {
	return Step{Note: note, Cmd: cmd, Show: cmd.String()}
}

// Replaced launchd jobs: the stack runs these now (plan section 3.8, step 4).
// Other com.cmf.* jobs (endpoint-probe, spark-tunnel) are the user's own and
// are only listed.
var replacedJobs = map[string]string{
	"com.cmf.mcp-server":     "the old host MCP server",
	"com.cmf.capture-poller": "the old host capture poller",
}

// Evaluate runs every check against the facts. It never runs commands; the
// fixes it returns are run by Apply.
func Evaluate(f probe.Facts) []Result {
	results := []Result{checkMode(f), checkDocker(f)}
	results = append(results, checkStack(f), checkPort(f), checkLaunchd(f), checkStrays(f), checkHostMCP(f),
		checkGraph(f), checkProviders(f), checkEmbedding(f), checkKnowledge(f), checkCapture(f),
		checkClients(f), checkConnector(f))
	results = append(results, checkApps(f)...)
	results = append(results, checkSelfTest(f))
	for i := range results {
		g := guide[results[i].ID]
		results[i].About, results[i].Feature, results[i].Doc = g.About, g.Feature, g.Doc
	}
	return results
}

func checkMode(f probe.Facts) Result {
	r := Result{ID: "mode", Title: "Install mode", Level: OK}
	if !f.Installed {
		self := f.Self
		if self == "" {
			self = "cmf"
		}
		r.Level, r.Detail = Fail, "CMF isn't installed in "+f.Home+" yet"
		r.Fix = &Fix{Label: "Install CMF here", Steps: []Step{step("write the stack's files into "+f.Home+" (nothing starts until you Apply features)", run.Cmd{Name: self, Args: []string{"install", "--yes"}})}}
		return r
	}
	if f.ModeErr != "" {
		r.Level, r.Detail = Fail, f.ModeErr
		return r
	}
	m := f.Mode.Mode
	r.Detail = fmt.Sprintf("standard · published server image · settings in %s", tilde(f.Home))
	if m.Mode == mode.Developer {
		r.Detail = fmt.Sprintf("developer · server built from %s · settings in %s", tilde(m.Repo), tilde(f.Home))
	}
	if !f.Mode.Consistent() {
		r.Level = Warn
		r.Detail += fmt.Sprintf("; but the compose files say %s. Run `cmf mode set %s` to make them agree", f.Mode.Detected.Mode, m.Mode)
	}
	if f.Health != nil && f.CmfVersion != "" && f.Health.Version != "" && BaseVersion(f.CmfVersion) != BaseVersion(f.Health.Version) {
		r.Level = Warn
		r.Detail += fmt.Sprintf("; this cmf is %s but the server is %s: use the cmf that matches the server (the release with the same number, or one built from the same checkout)", f.CmfVersion, f.Health.Version)
	}
	return r
}

// BaseVersion drops a leading "v" and a local build's "-dev", so 0.1.4,
// v0.1.4 and 0.1.4-dev count as the same version.
func BaseVersion(v string) string {
	return strings.TrimSuffix(strings.TrimPrefix(v, "v"), "-dev")
}

func checkDocker(f probe.Facts) Result {
	r := Result{ID: "docker", Title: "Docker installed and running"}
	d := f.Docker
	switch {
	case d.Responding:
		r.Level, r.Detail = OK, "Docker "+d.Version
	case !d.Installed:
		r.Level, r.Detail = Fail, "Docker isn't installed. Install Docker Desktop (or OrbStack / Colima on macOS): https://docs.docker.com/get-docker/"
	case d.Hung:
		r.Level, r.Detail = Fail, "Docker is running but not answering (the daemon is hung). Restarting Docker Desktop fixes it; containers come back on their own."
		r.Fix = restartDockerFix(f.OS)
	default:
		r.Level, r.Detail = Fail, "Docker isn't running ("+d.Error+")"
		r.Fix = startDockerFix(f.OS)
	}
	return r
}

func startDockerFix(goos string) *Fix {
	switch goos {
	case "darwin":
		return &Fix{Label: "Start Docker Desktop", Steps: []Step{step("open Docker Desktop", run.Cmd{Name: "open", Args: []string{"-a", "Docker"}})}}
	case "windows":
		return &Fix{Label: "Start Docker Desktop", Steps: []Step{step("open Docker Desktop", run.Cmd{Name: "cmd", Args: []string{"/c", "start", "", `C:\Program Files\Docker\Docker\Docker Desktop.exe`}})}}
	}
	return &Fix{Label: "Start Docker", Steps: []Step{step("start the Docker service", run.Cmd{Name: "systemctl", Args: []string{"--user", "start", "docker-desktop"}})}}
}

// restartDockerFix quits Docker Desktop, force-quits its backend only if it
// ignores the request (a hung daemon does), and opens it again.
func restartDockerFix(goos string) *Fix {
	if goos != "darwin" {
		return startDockerFix(goos)
	}
	script := `osascript -e 'quit app "Docker Desktop"'; ` +
		`for i in $(seq 1 30); do pgrep -f com.docker.backend >/dev/null || break; sleep 1; done; ` +
		`pgrep -f com.docker.backend >/dev/null && { pkill -TERM -f com.docker.backend; sleep 5; pkill -KILL -f com.docker.backend; }; ` +
		`open -a Docker`
	return &Fix{Label: "Restart Docker Desktop", Steps: []Step{
		step("quit Docker Desktop (force-quit its backend only if it ignores that) and open it again", run.Cmd{Name: "sh", Args: []string{"-c", script}, Timeout: 90 * time.Second}),
	}}
}

func needsDocker(r Result, f probe.Facts) (Result, bool) {
	if f.Docker.Responding {
		return r, false
	}
	r.Level, r.Detail = NA, "needs Docker"
	return r, true
}

func compose(f probe.Facts, args ...string) run.Cmd {
	return run.Cmd{Name: "docker", Args: append([]string{"compose", "--project-directory", f.Home}, args...), Timeout: 10 * time.Minute}
}

// wantedServices are the stack's services that should be running.
func wantedServices(f probe.Facts) []string {
	services := []string{"falkordb", "cmf-server"}
	if f.HasProfile("capture") {
		services = append(services, "cmf-poller")
	}
	return services
}

func checkStack(f probe.Facts) Result {
	r := Result{ID: "stack", Title: "CMF server and FalkorDB containers"}
	if r, skip := needsDocker(r, f); skip {
		return r
	}
	var missing, unhealthy, fine []string
	for _, svc := range wantedServices(f) {
		c := f.Service(svc)
		switch {
		case c == nil || c.State != "running":
			missing = append(missing, svc)
		case c.Health == "unhealthy":
			unhealthy = append(unhealthy, svc)
		default:
			fine = append(fine, svc)
		}
	}
	switch {
	case len(missing) > 0:
		r.Level = Fail
		r.Detail = "not running: " + strings.Join(missing, ", ")
		if c := f.Service("cmf-server"); c != nil && (c.State == "restarting" || c.State == "exited") {
			// Usually the startup config check (item 1.1) refusing to serve.
			r.Detail += "; cmf-server keeps stopping, and `cmf logs cmf-server --errors` says why"
		}
		r.Fix = &Fix{Label: "Start the stack", Steps: []Step{step("docker compose up -d in "+f.Home, compose(f, "up", "-d"))}}
	case len(unhealthy) > 0:
		r.Level = Fail
		r.Detail = "unhealthy: " + strings.Join(unhealthy, ", ")
		r.Fix = &Fix{Label: "Recreate " + strings.Join(unhealthy, ", "), Steps: []Step{
			step("recreate the unhealthy containers", compose(f, append([]string{"up", "-d", "--force-recreate"}, unhealthy...)...)),
		}}
	default:
		r.Level, r.Detail = OK, "running: "+strings.Join(fine, ", ")
	}
	return r
}

func checkPort(f probe.Facts) Result {
	r := Result{ID: "port", Title: fmt.Sprintf("Exactly one CMF on port %d", f.Port)}
	switch {
	case f.Listener == nil && f.OS == "windows":
		r.Level, r.Detail = NA, "port owner check comes with the Windows pass"
	case f.Listener == nil:
		r.Level, r.Detail = Fail, "nothing is listening; the CMF server container isn't serving"
	case !f.Listener.IsDocker():
		r.Level = Fail
		r.Detail = fmt.Sprintf("%s (PID %d) has the port, not the CMF container. Stop it, then start the stack", f.Listener.Command, f.Listener.PID)
	case !f.Docker.Responding:
		r.Level, r.Detail = NA, fmt.Sprintf("Docker holds the port; which container can't be checked while Docker isn't answering")
	case f.Service("cmf-server") == nil || f.Service("cmf-server").State != "running":
		r.Level, r.Detail = Warn, "Docker holds the port, but not for this stack's cmf-server (an older CMF stack?)"
	default:
		r.Level, r.Detail = OK, "the cmf-server container"
	}
	return r
}

func checkLaunchd(f probe.Facts) Result {
	r := Result{ID: "launchd", Title: "No leftover launchd services"}
	if f.OS != "darwin" {
		r.Level, r.Detail = NA, "macOS only (systemd and Task Scheduler come later)"
		return r
	}
	var conflicts, others []string
	var steps []Step
	for _, job := range f.Launchd {
		purpose, replaced := replacedJobs[job.Label]
		state := "disabled"
		if !job.Disabled {
			state = "enabled"
		}
		if job.Loaded {
			state += ", loaded"
		}
		if !replaced {
			others = append(others, job.Label+" ("+state+")")
			continue
		}
		if job.Loaded || !job.Disabled {
			conflicts = append(conflicts, fmt.Sprintf("%s, %s (%s)", job.Label, purpose, state))
			target := "gui/$(id -u)/" + job.Label
			steps = append(steps, step("unload and disable "+job.Label+" (its plist stays)",
				run.Cmd{Name: "sh", Args: []string{"-c", fmt.Sprintf("launchctl bootout %s 2>/dev/null; launchctl disable %s", target, target)}}))
		}
	}
	if len(conflicts) > 0 {
		r.Level = Fail
		r.Detail = "replaced by the stack but still active: " + strings.Join(conflicts, "; ")
		r.Fix = &Fix{Label: "Disable the replaced services", Steps: steps}
		return r
	}
	r.Level, r.Detail = OK, "the old MCP server and poller jobs are disabled"
	if len(others) > 0 {
		r.Detail += "; yours, left alone: " + strings.Join(others, ", ")
	}
	return r
}

func checkStrays(f probe.Facts) Result {
	r := Result{ID: "containers", Title: "No CMF containers from older installs running"}
	if r, skip := needsDocker(r, f); skip {
		return r
	}
	var running, stopped []string
	var steps []Step
	for _, c := range f.Strays {
		if c.State == "running" {
			running = append(running, c.Name)
			steps = append(steps, step("stop "+c.Name+" (not removed; its data stays)", run.Cmd{Name: "docker", Args: []string{"stop", c.Name}, Timeout: time.Minute}))
		} else {
			stopped = append(stopped, c.Name)
		}
	}
	if len(running) > 0 {
		r.Level = Warn
		r.Detail = "running outside the ~/.cmf stack: " + strings.Join(running, ", ")
		r.Fix = &Fix{Label: "Stop them", Steps: steps}
		return r
	}
	r.Level, r.Detail = OK, "none running"
	if len(stopped) > 0 {
		r.Detail += fmt.Sprintf(" (%d stopped, kept: %s)", len(stopped), strings.Join(stopped, ", "))
	}
	return r
}

func checkHostMCP(f probe.Facts) Result {
	r := Result{ID: "host-server", Title: "No CMF server running outside Docker"}
	if f.OS == "linux" {
		r.Level, r.Detail = NA, "not checked on Linux yet"
		return r
	}
	if len(f.HostMCP) == 0 {
		r.Level, r.Detail = OK, "none"
		return r
	}
	var procs []string
	for _, p := range f.HostMCP {
		procs = append(procs, fmt.Sprintf("PID %d", p.PID))
	}
	r.Level = Warn
	r.Detail = "server.mcp is running on this computer (" + strings.Join(procs, ", ") + "), e.g. a stdio entry in an AI app or a debug run. cmf doesn't stop it; quit it if it isn't yours on purpose"
	return r
}

func healthMissing(r Result, f probe.Facts) (Result, bool) {
	if f.Health != nil {
		return r, false
	}
	r.Level, r.Detail = Fail, "the server doesn't answer /health: "+f.HealthErr
	if f.Docker.Responding && f.Service("cmf-server") != nil && f.Service("cmf-server").State == "running" {
		r.Fix = &Fix{Label: "Restart the CMF server", Steps: []Step{step("restart cmf-server", compose(f, "restart", "cmf-server"))}}
	}
	return r, true
}

func checkGraph(f probe.Facts) Result {
	r := Result{ID: "graph", Title: "FalkorDB reachable, graph has memories"}
	if r, missing := healthMissing(r, f); missing {
		return r
	}
	h := f.Health
	switch {
	case !h.FalkorDB.Reachable:
		r.Level, r.Detail = Fail, "the server can't reach FalkorDB at "+h.FalkorDB.Address+" "+h.FalkorDB.Error
		if f.Docker.Responding {
			r.Fix = &Fix{Label: "Recreate FalkorDB", Steps: []Step{step("recreate falkordb", compose(f, "up", "-d", "--force-recreate", "falkordb"))}}
		}
	case !h.FalkorDB.GraphExists:
		r.Level, r.Detail = Warn, fmt.Sprintf("graph %q doesn't exist yet; it's created by the first memory", h.Graph)
	case f.Nodes >= 0:
		r.Level, r.Detail = OK, fmt.Sprintf("graph %q, %d nodes", h.Graph, f.Nodes)
	default:
		r.Level, r.Detail = OK, fmt.Sprintf("graph %q (node count unavailable: %s)", h.Graph, f.NodesErr)
	}
	return r
}

func checkProviders(f probe.Facts) Result {
	r := Result{ID: "providers", Title: "Model providers answer"}
	if r, missing := healthMissing(r, f); missing {
		r.Fix = nil
		return r
	}
	h := f.Health
	small := ""
	if c := h.LLMContext; c != nil && c.Local && c.Tokens > 0 && c.Tokens < c.RecommendedMin {
		small = smallContextLine(c)
	}
	if h.ProviderTests == nil {
		p := h.Providers
		r.Level, r.Detail = NA, fmt.Sprintf("LLM %s, embeddings %s, capture %s; this server is older than the live provider test; update it to see whether each one answers", p.LLM, p.Embeddings, p.CaptureLLM)
		if small != "" {
			r.Level, r.Detail = Warn, small
		}
		return r
	}
	var bad, good []string
	for _, t := range h.ProviderTests {
		s := fmt.Sprintf("%s %s: %s", t.Role, t.Provider, t.Detail)
		if t.OK {
			good = append(good, fmt.Sprintf("%s %s", t.Role, t.Provider))
		} else {
			bad = append(bad, s)
		}
	}
	var warnings []string
	if h.ConfigCheck != nil {
		warnings = h.ConfigCheck.Warnings
	}
	if small != "" {
		warnings = append(warnings, small)
	}
	switch {
	case len(bad) > 0:
		r.Level, r.Detail = Fail, strings.Join(bad, "; ")
	case len(warnings) > 0:
		r.Level, r.Detail = Warn, strings.Join(warnings, "; ")
	default:
		r.Level, r.Detail = OK, strings.Join(good, ", ")+" answer"
	}
	return r
}

// smallContextLine: a local model loaded with too small a context makes
// longer saves and poller windows fail or be cut down (B18/B20).
func smallContextLine(c *probe.LLMContext) string {
	model := c.Model
	if model == "" {
		model = "the local model"
	}
	return fmt.Sprintf("%s is loaded with a %d-token context; CMF needs at least %d (32768 recommended), so long memories are split and some conversations can't be read. Reload it with a larger context in LM Studio", model, c.Tokens, c.RecommendedMin)
}

// MemoryRetryCmd re-saves failed memories that are still kept (B20).
func MemoryRetryCmd(home string) run.Cmd {
	return run.Cmd{Name: "docker", Args: []string{"compose", "--project-directory", home, "exec", "-T", "cmf-server", "python", "-m", "server.memory_writes", "retry"},
		Timeout: 30 * time.Minute}
}

func checkEmbedding(f probe.Facts) Result {
	r := Result{ID: "embedding", Title: "Embedding width matches the graph"}
	if r, missing := healthMissing(r, f); missing {
		r.Fix = nil
		return r
	}
	e := f.Health.Embedding
	switch {
	case e == nil:
		r.Level, r.Detail = NA, fmt.Sprintf("configured width %d; this server is older than the graph comparison; update it to check the graph's width", f.Health.Providers.EmbeddingDim)
	case e.Error != "":
		r.Level, r.Detail = Warn, fmt.Sprintf("configured width %d; couldn't read the graph's: %s", e.Configured, e.Error)
	case e.Graph == nil:
		r.Level, r.Detail = OK, fmt.Sprintf("the graph has no vectors yet; new ones will be %d wide", e.Configured)
	case *e.Graph == e.Configured:
		r.Level, r.Detail = OK, fmt.Sprintf("%d, the same as the graph's vectors", e.Configured)
	default:
		r.Level, r.Detail = Fail, fmt.Sprintf("EMBEDDING_DIM is %d but graph %q holds %d-wide vectors; set it back to %d with the embedder that made them, or use a new graph", e.Configured, f.Health.Graph, *e.Graph, *e.Graph)
	}
	return r
}

func checkKnowledge(f probe.Facts) Result {
	r := Result{ID: "knowledge", Title: "Knowledge sources"}
	if r, missing := healthMissing(r, f); missing {
		r.Fix = nil
		return r
	}
	if len(f.Health.KnowledgeSources) == 0 {
		r.Level, r.Detail = OK, "none (memory only)"
		return r
	}
	r.Level, r.Detail = OK, strings.Join(f.Health.KnowledgeSources, ", ")+" (configured; CMF doesn't test that it can reach them yet)"
	return r
}

func checkCapture(f probe.Facts) Result {
	r := Result{ID: "capture", Title: "Background capture running"}
	if !f.HasProfile("capture") {
		r.Level, r.Detail = Warn, "capture is off (no \"capture\" in COMPOSE_PROFILES, removed by hand); any Apply in Features turns it back on"
		return r
	}
	if r, skip := needsDocker(r, f); skip {
		return r
	}
	poller := f.Service("cmf-poller")
	if poller == nil || poller.State != "running" {
		r.Level, r.Detail = Fail, "the cmf-poller container isn't running"
		r.Fix = &Fix{Label: "Start the poller", Steps: []Step{step("start cmf-poller", compose(f, "up", "-d", "cmf-poller"))}}
		return r
	}
	r.Level, r.Detail = OK, "cmf-poller is running"
	var reading []string
	for _, s := range f.Capture {
		if s.Poller {
			reading = append(reading, s.Label)
		}
	}
	if f.Capture != nil {
		if len(reading) == 0 {
			r.Level, r.Detail = Warn, "cmf-poller is running but reads no transcript folder: no AI app's transcripts were found on this computer, or the CMF_CAPTURE_*_DIR settings are unset (any Apply sets them)"
			return r
		}
		r.Detail += "; reading " + strings.Join(reading, ", ")
	}
	if f.Health == nil {
		return r
	}
	var failed []probe.BackgroundWrite
	for _, w := range f.Health.BackgroundWrites {
		if w.Status == "failed" {
			failed = append(failed, w)
		}
	}
	if len(failed) > 0 {
		latest := failed[0] // /health lists them newest first
		kind := latest.ErrorKind
		if kind == "" {
			kind = "no reason recorded"
		}
		r.Level = Warn
		r.Detail = fmt.Sprintf("%s; %d of the last %d background writes failed, latest: %s", r.Detail, len(failed), len(f.Health.BackgroundWrites), kind)
	} else if n := len(f.Health.BackgroundWrites); n > 0 {
		r.Detail += fmt.Sprintf("; last %d background writes committed", n)
	}
	if n := len(f.Health.RetryableWrites); n > 0 {
		r.Level = Warn
		r.Detail += fmt.Sprintf("; %d failed memory save(s) kept for a retry", n)
		r.Fix = &Fix{Label: "Retry failed saves", Steps: []Step{step("save the kept memories again (a minute or two each on a local model)", MemoryRetryCmd(f.Home))}}
	}
	return r
}

func checkClients(f probe.Facts) Result {
	r := Result{ID: "clients", Title: "Each AI app has exactly one CMF entry"}
	if f.Clients == nil {
		r.Level, r.Detail = NA, "not checked"
		return r
	}
	var bad, good []string
	for _, s := range f.Clients {
		cmf := s.CMF()
		switch {
		case s.Err != "":
			bad = append(bad, s.Label+": "+s.Err)
		case !s.Installed:
		case len(cmf) == 0:
		case s.Healthy():
			good = append(good, s.Label)
		case len(cmf) > 1:
			bad = append(bad, fmt.Sprintf("%s has %d CMF entries", s.Label, len(cmf)))
		default:
			bad = append(bad, fmt.Sprintf("%s's CMF entry %q %s", s.Label, cmf[0].Name, cmf[0].Problem))
		}
	}
	tail := "" // apps with no entry: their own rows below say so
	switch {
	case len(bad) > 0:
		r.Level, r.Detail = Fail, strings.Join(bad, "; ")+tail
		self := f.Self
		if self == "" {
			self = "cmf"
		}
		r.Fix = &Fix{Label: "Keep one working CMF entry per app", Steps: []Step{
			step("rewrite the affected apps' configs (backups kept)", run.Cmd{Name: self, Args: []string{"clients", "fix", "--yes"}}),
		}}
	case len(good) > 0:
		r.Level, r.Detail = OK, "connected: "+strings.Join(good, ", ")+tail
	default:
		r.Level, r.Detail = OK, "no app has a local CMF entry"+tail
	}
	return r
}

// checkConnector: the claude.ai connector, the recommended way for Claude
// Desktop (Chat, Cowork, Code tab) and Claude Code to reach CMF. It needs
// remote access (a public address with OAuth); it's active when Claude calls
// came in through it in the last day.
func checkConnector(f probe.Facts) Result {
	r := Result{ID: "connector", Title: "claude.ai connector"}
	addr := strings.TrimRight(f.Env["CMF_MCP_ISSUER_URL"], "/")
	if addr == "" {
		r.Level, r.Detail = NA, "not set up: it needs remote access first (`cmf remote enable tailscale`), then the connector added in claude.ai"
		return r
	}
	var last time.Time
	if f.Health != nil {
		for _, c := range f.Health.ClientsSeen {
			if c.Harness == "claude_desktop" && c.LastSeen.After(last) {
				last = c.LastSeen
			}
		}
	}
	if rc, ok := refusedSince(f, last, func(h string) bool { return strings.HasPrefix(h, "claude") && h != "claude_code" }); ok {
		r.Level, r.Detail = Warn, refusedLine(f, rc)+"\naddress     "+addr+"/mcp\nreconnect it in claude.ai → Settings → Connectors (Disconnect, then Connect), then start a new conversation"
		return r
	}
	if last.IsZero() {
		r.Level, r.Detail = NA, "remote access is on, but no Claude call came through a connector in the last day\naddress     "+addr+"/mcp\nadd it in claude.ai → Settings → Connectors, then start a new conversation"
		return r
	}
	r.Level, r.Detail = OK, "active\naddress     "+addr+"/mcp\nlast call   "+ago(f.At.Sub(last))+"\ncovers      Claude Desktop (Chat, Cowork, Code tab) and Claude Code"
	return r
}

// appRows are the AI apps Status shows one row each for: the app's own
// CMF entry (clients), the transcript source the poller reads (capture), and
// the names its calls arrive under (the journal's harness).
var appRows = []struct {
	ID, Title, Client, Source string
	Seen                      []string
}{
	{"app-claude-code", "Claude Code (CLI and Code tab)", "claude-code", "claude_code", []string{"claude_code", "claude_desktop_code"}},
	{"app-claude-desktop", "Claude Desktop (Chat and Cowork)", "claude-desktop", "claude_cowork", []string{"claude_desktop", "claude_cowork"}},
	{"app-codex", "Codex", "codex", "codex", []string{"codex", "codex_mcp_client"}},
	{"app-antigravity", "Antigravity", "antigravity", "antigravity", []string{"antigravity"}},
	{"app-cursor", "Cursor", "cursor", "", []string{"cursor"}},
	{"app-vscode", "VS Code", "vscode", "", []string{"vscode", "visual_studio_code", "copilot"}},
}

var hookNames = map[string]string{
	"plugin": "the cmf-capture plugin (Python)",
	"cmf":    "cmf hook stop",
	"python": "the older Python checkpoint hook",
}

// checkApps is one row per AI app: whether it's connected to CMF, its
// checkpoint hook, and the transcript folder the poller reads. OK means the
// app is on; n/a means it's off or not on this computer.
func checkApps(f probe.Facts) []Result {
	clientByID := map[string]clients.State{}
	for _, s := range f.Clients {
		clientByID[s.ID] = s
	}
	sourceByID := map[string]capture.SourceState{}
	for _, s := range f.Capture {
		sourceByID[s.ID] = s
	}
	// A claude.ai connector belongs to the account, so every Claude app
	// signed in to it has it; its calls arrive named claude_desktop.
	connector := false
	if d, ok := clientByID["claude-desktop"]; ok && len(d.CMF()) == 0 && f.Health != nil {
		for _, c := range f.Health.ClientsSeen {
			connector = connector || c.Harness == "claude_desktop"
		}
	}
	var out []Result
	for _, a := range appRows {
		r := Result{ID: a.ID, Title: a.Title}
		var lines []string
		connected, installed, noEntry := false, a.Client == "", false
		if c, ok := clientByID[a.Client]; ok {
			installed = c.Installed
			cmf := c.CMF()
			switch {
			case c.Err != "":
				lines = append(lines, "connection  can't read "+tilde(c.Path)+": "+c.Err)
			case !c.Installed:
				lines = append(lines, "connection  app not on this computer")
			case len(cmf) == 0:
				noEntry = true
				lines = append(lines, "connection  no CMF entry in "+tilde(c.Path))
			default:
				where := "local"
				if !strings.Contains(cmf[0].URL, "127.0.0.1") && !strings.Contains(cmf[0].URL, "localhost") {
					where = "public URL"
				}
				state := where + " entry"
				if !c.Healthy() {
					state = fmt.Sprintf("%d CMF entries, see the row below", len(cmf))
					if len(cmf) == 1 {
						state = "an entry that " + cmf[0].Problem + ","
					}
				}
				connected = c.Healthy()
				lines = append(lines, "connection  "+state+" in "+tilde(c.Path))
			}
		}
		if noEntry && connector && a.ID == "app-claude-code" {
			lines[0] = "connection  the claude.ai connector (its calls show under Claude Desktop)"
			connected = true
		}
		instrProblem := ""
		if line, problem := instructionsLine(f, a.Client, a.Seen); line != "" {
			lines = append(lines, "instructions "+line)
			instrProblem = problem
		}
		hookProblem, hookUpdatable := "", false
		if s, ok := sourceByID[a.Source]; ok {
			installed = installed || s.Present
			serverVersion := ""
			if f.Health != nil {
				serverVersion = f.Health.Version
			}
			capture.DescribeHook(&s, serverVersion, contextmemoryfabric.PluginVersion())
			hookProblem, hookUpdatable = s.HookProblem, s.HookUpdatable
			hook := hookNames[s.Hook]
			if hook == "" {
				hook = "none"
			}
			if strings.HasPrefix(s.HookLine, "checkpoint hook: ") && s.Hook != "" {
				hook = strings.TrimPrefix(s.HookLine, "checkpoint hook: ")
			}
			if s.Worker {
				hook += "; plus an older host worker hook (the poller does its job)"
			}
			lines = append(lines, "hook        "+hook)
			switch {
			case s.Poller:
				poll := tilde(s.Folder)
				if !s.Last.IsZero() {
					poll += " · last transcript " + ago(f.At.Sub(s.Last))
				}
				lines = append(lines, "poller      "+poll)
			case s.Present && s.Folder == "":
				lines = append(lines, "poller      not pointed at its folder ("+s.EnvKey+" isn't set)")
			case s.Present:
				lines = append(lines, "poller      off")
			}
		}
		if f.Health != nil {
			var last time.Time
			for _, c := range f.Health.ClientsSeen {
				if slices.Contains(a.Seen, c.Harness) && c.LastSeen.After(last) {
					last = c.LastSeen
				}
			}
			if n, at := liveCaptures(f, a.Seen); n > 0 {
				lines = append(lines, fmt.Sprintf("captures    %d in the last 7 days (latest %s)", n, ago(f.At.Sub(at))))
			}
			if !last.IsZero() {
				lines = append(lines, "seen        "+ago(f.At.Sub(last)))
				if noEntry {
					// Called CMF without a local entry: a claude.ai connector
					// (or another remote one) reaches it over the public URL.
					lines[0] = "connection  the claude.ai connector (no local entry in " + tilde(clientByID[a.Client].Path) + ")"
				}
				connected = true
			}
		}
		r.Detail = strings.Join(lines, "\n")
		var seen time.Time
		if f.Health != nil {
			for _, c := range f.Health.ClientsSeen {
				if slices.Contains(a.Seen, c.Harness) && c.LastSeen.After(seen) {
					seen = c.LastSeen
				}
			}
		}
		if rc, ok := refusedSince(f, seen, func(h string) bool { return slices.Contains(a.Seen, h) }); ok {
			// Its entry looks fine, but the server turns its calls away: a
			// lapsed sign-in or an old token. Rewriting the entry with this
			// computer's address and token fixes both.
			r.Level, r.Detail = Warn, refusedLine(f, rc)+"\n"+r.Detail
			if c, ok := clientByID[a.Client]; ok && c.Installed && c.Err == "" {
				self := f.Self
				if self == "" {
					self = "cmf"
				}
				r.Fix = &Fix{Label: "Use this computer's address", Steps: []Step{
					step("point "+a.Title+" at this computer's CMF address with its access token (backup kept; creates the token and restarts the server if there isn't one yet)",
						run.Cmd{Name: self, Args: []string{"clients", "set", a.Client, "--yes"}}),
				}}
			}
			out = append(out, r)
			continue
		}
		if !connected && !installed {
			continue // not on this computer: the Features page still lists it
		}
		r.Level = NA // installed, not connected
		if connected {
			r.Level = OK
			if instrProblem != "" && hookProblem == "" {
				r.Level, r.Detail = Warn, instrProblem+"\n"+r.Detail
				r.Fix = addInstructionsFix(f, a.Client)
			}
			if hookProblem != "" {
				// Connected, but its checkpoints run an old cmf or plugin (v0.1.5).
				r.Level, r.Detail = Warn, hookProblem+"\n"+r.Detail
				if hookUpdatable {
					self := f.Self
					if self == "" {
						self = "cmf"
					}
					r.Fix = &Fix{Label: "Update the hook's cmf", Steps: []Step{
						step("copy this cmf over the older one the checkpoint hook runs (backup kept)",
							run.Cmd{Name: self, Args: []string{"capture", "hook", "update", "--yes"}}),
					}}
				}
			}
		} else {
			r.Detail = "not connected\n" + r.Detail
			if instrProblem != "" {
				r.Action = addInstructionsFix(f, a.Client)
			}
		}
		out = append(out, r)
	}
	return out
}

// instructionsLine says whether an app's own instructions carry CMF's
// guidance (v0.1.7): read from its file where there is one (Claude Code,
// Codex); for Claude's account instructions, which CMF can't read, whether
// the app has been capturing instead. problem is set when a file lacks it.
func instructionsLine(f probe.Facts, client string, harnesses []string) (line, problem string) {
	if file, ok := f.Instructions[client]; ok {
		where := tilde(file.Path)
		switch file.State {
		case "current":
			return "CMF's instructions in " + where, ""
		case "partial":
			return "an older or trimmed CMF block in " + where + " (missing: " + strings.Join(file.Missing, ", ") + ")",
				"its instructions in " + where + " don't name " + strings.Join(file.Missing, ", ") + "; paste the current block (Features → Copy instructions)"
		}
		return "none in " + where, "no CMF instructions in " + where + "; paste them from Features → Copy instructions"
	}
	if client == "claude-desktop" {
		if n, at := liveCaptures(f, harnesses); n > 0 {
			return fmt.Sprintf("in your claude.ai account (CMF can't read them); working: %d captures in the last 7 days, latest %s", n, ago(f.At.Sub(at))), ""
		}
		return "in your claude.ai account (CMF can't read them); no captures from Chat or Cowork in the last 7 days, so check Settings → Account → Instructions for Claude and Cowork's cmf-capture plugin", ""
	}
	return "", ""
}

// addInstructionsFix writes CMF's instruction block into the app's file.
func addInstructionsFix(f probe.Facts, client string) *Fix {
	file, ok := f.Instructions[client]
	if !ok {
		return nil
	}
	self := f.Self
	if self == "" {
		self = "cmf"
	}
	return &Fix{Label: "Add CMF's instructions", Steps: []Step{
		step("write CMF's instruction block into "+tilde(file.Path)+" (between cmf:begin/end markers, backup kept)",
			run.Cmd{Name: self, Args: []string{"instructions", "add", client, "--yes"}}),
	}}
}

// liveCaptures totals the capture_session items the harnesses staged in the last 7 days.
func liveCaptures(f probe.Facts, harnesses []string) (int, time.Time) {
	if f.Health == nil {
		return 0, time.Time{}
	}
	n, last := 0, time.Time{}
	for _, h := range harnesses {
		if c, ok := f.Health.LiveCaptures[h]; ok {
			n += c.Count
			if c.Last.After(last) {
				last = c.Last
			}
		}
	}
	return n, last
}

// refusedSince is the latest refusal of a harness match accepts that came
// after the app's last successful call (seen), if any.
func refusedSince(f probe.Facts, seen time.Time, match func(string) bool) (probe.RefusedCall, bool) {
	var best probe.RefusedCall
	if f.Health == nil {
		return best, false
	}
	for _, rc := range f.Health.RefusedCalls {
		// Refusals the app got past (Codex tries an old sign-in before its
		// token on each connect, B21) aren't a lockout.
		if rc.LastOKAt != nil && !rc.LastOKAt.Before(rc.LastAt) {
			continue
		}
		if match(rc.Harness) && rc.LastAt.After(seen) && rc.LastAt.After(best.LastAt) {
			best = rc
		}
	}
	return best, !best.LastAt.IsZero()
}

func refusedLine(f probe.Facts, rc probe.RefusedCall) string {
	calls := "call"
	if rc.Count != 1 {
		calls = "calls"
	}
	return fmt.Sprintf("CMF refused %d %s in the last day (last %s): its sign-in or access token isn't valid any more", rc.Count, calls, ago(f.At.Sub(rc.LastAt)))
}

func ago(d time.Duration) string {
	switch {
	case d < time.Minute:
		return "just now"
	case d < time.Hour:
		return fmt.Sprintf("%d min ago", int(d.Minutes()))
	case d < 48*time.Hour:
		return fmt.Sprintf("%d h ago", int(d.Hours()))
	}
	return fmt.Sprintf("%d days ago", int(d.Hours()/24))
}

// SelftestCmd runs the server's self-test (item 1.7) inside the stack.
// It calls the real providers; extraction on a local model takes minutes.
func SelftestCmd(home string) run.Cmd {
	return run.Cmd{Name: "docker", Args: []string{"compose", "--project-directory", home, "exec", "-T", "cmf-server", "python", "-m", "server.selftest"},
		Timeout: 7 * time.Minute}
}

func checkSelfTest(f probe.Facts) Result {
	r := Result{ID: "selftest", Title: "Self-test (write, recall, clean up)"}
	if r, missing := healthMissing(r, f); missing {
		r.Fix = nil
		return r
	}
	t := f.Health.Selftest
	rerun := &Fix{Label: "Run self-test", Steps: []Step{step("write, recall and delete a test memory in a scratch graph (a few minutes)", SelftestCmd(f.Home))}}
	switch {
	case t == nil:
		r.Level, r.Detail = NA, "not run yet; `cmf selftest` runs it (a few minutes, calls your providers once)"
	case t.OK:
		r.Level, r.Detail = OK, fmt.Sprintf("passed %s (%.0f s)", ago(f.At.Sub(t.At)), t.Seconds)
	default:
		r.Level, r.Detail, r.Fix = Fail, fmt.Sprintf("%s failed %s: %s", t.FailedStage, ago(f.At.Sub(t.At)), t.FailedDetail), rerun
	}
	if r.Fix == nil {
		r.Action = rerun
	}
	return r
}

// Worst is the most severe level among results (n/a counts as ok).
func Worst(results []Result) Level {
	worst := OK
	for _, r := range results {
		if r.Level == Fail {
			return Fail
		}
		if r.Level == Warn {
			worst = Warn
		}
	}
	return worst
}

// ApplyFix runs a fix's steps in order and stops at the first failure.
func ApplyFix(ctx context.Context, runner run.Runner, fix *Fix, log func(string)) error {
	for _, s := range fix.Steps {
		log("  - " + s.Note)
		if res := runner.Run(ctx, s.Cmd); !res.OK() {
			out := res.Output()
			if out != "" {
				out = ": " + out
			}
			return fmt.Errorf("%s failed (%v)%s", s.Show, res.Err, out)
		} else if text := strings.TrimSpace(res.Stdout); text != "" {
			// What the step says it did, e.g. cmf's "restart Codex" note.
			for _, line := range strings.Split(text, "\n") {
				log("    " + line)
			}
		}
	}
	return nil
}
