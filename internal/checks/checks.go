// Package checks turns probe.Facts into the Status rows of plan section 3.3:
// each green / yellow / red with a one-line explanation and, where cmf can
// do it, a Fix. Rows the server can't answer yet (an older server) show as
// "n/a".
package checks

import (
	"context"
	"fmt"
	"os"
	"sort"
	"strings"
	"time"

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
	"apps-seen": {About: "Apps that called CMF in the last day. One that's set up but not seen usually needs a restart and a new conversation.",
		Feature: "group:apps"},
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
		checkClients(f), checkAppsSeen(f), checkSelfTest(f))
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
	return r
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
	if h.ProviderTests == nil {
		p := h.Providers
		r.Level, r.Detail = NA, fmt.Sprintf("LLM %s, embeddings %s, capture %s; this server is older than the live provider test; update it to see whether each one answers", p.LLM, p.Embeddings, p.CaptureLLM)
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
		r.Level, r.Detail = Warn, "capture is off (no \"capture\" in COMPOSE_PROFILES)"
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
	return r
}

func checkClients(f probe.Facts) Result {
	r := Result{ID: "clients", Title: "Each AI app has exactly one CMF entry"}
	if f.Clients == nil {
		r.Level, r.Detail = NA, "not checked"
		return r
	}
	var bad, good, unset []string
	for _, s := range f.Clients {
		cmf := s.CMF()
		switch {
		case s.Err != "":
			bad = append(bad, s.Label+": "+s.Err)
		case !s.Installed:
		case len(cmf) == 0:
			unset = append(unset, s.ID)
		case s.Healthy():
			good = append(good, s.Label)
		case len(cmf) > 1:
			bad = append(bad, fmt.Sprintf("%s has %d CMF entries", s.Label, len(cmf)))
		default:
			bad = append(bad, fmt.Sprintf("%s's CMF entry %q doesn't reach this install", s.Label, cmf[0].Name))
		}
	}
	tail := ""
	if len(unset) > 0 {
		tail = "; no entry: " + strings.Join(unset, ", ") + " (`cmf clients set <app>` adds one; apps using a claude.ai connector don't need one)"
	}
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

func checkAppsSeen(f probe.Facts) Result {
	r := Result{ID: "apps-seen", Title: "AI apps seen in the last 24 hours"}
	if r, missing := healthMissing(r, f); missing {
		r.Fix = nil
		return r
	}
	var seen []string
	clients := append([]probe.ClientSeen(nil), f.Health.ClientsSeen...)
	sort.Slice(clients, func(i, j int) bool { return clients[i].LastSeen.After(clients[j].LastSeen) })
	for _, c := range clients {
		if f.At.Sub(c.LastSeen) <= 24*time.Hour {
			seen = append(seen, fmt.Sprintf("%s (%s)", c.Harness, ago(f.At.Sub(c.LastSeen))))
		}
	}
	if len(seen) == 0 {
		r.Level = Warn
		r.Detail = "no AI app has called CMF since the server started or in the last 24 h. Quit and reopen the app, then start a new session"
		return r
	}
	r.Level, r.Detail = OK, strings.Join(seen, ", ")
	return r
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
	rerun := &Fix{Label: "Run the self-test", Steps: []Step{step("write, recall and delete a test memory in a scratch graph (a few minutes)", SelftestCmd(f.Home))}}
	switch {
	case t == nil:
		r.Level, r.Detail = NA, "not run yet; `cmf selftest` runs it (a few minutes, calls your providers once)"
	case t.OK:
		r.Level, r.Detail = OK, fmt.Sprintf("passed %s (%.0f s)", ago(f.At.Sub(t.At)), t.Seconds)
	default:
		r.Level, r.Detail, r.Fix = Fail, fmt.Sprintf("%s failed %s: %s", t.FailedStage, ago(f.At.Sub(t.At)), t.FailedDetail), rerun
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
		}
	}
	return nil
}
