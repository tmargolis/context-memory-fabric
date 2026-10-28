package main

import (
	"context"
	"crypto/rand"
	"crypto/subtle"
	"embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"net"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"time"

	contextmemoryfabric "github.com/tmargolis/context-memory-fabric"
	"github.com/tmargolis/context-memory-fabric/internal/capture"
	"github.com/tmargolis/context-memory-fabric/internal/checks"
	"github.com/tmargolis/context-memory-fabric/internal/clients"
	"github.com/tmargolis/context-memory-fabric/internal/diag"
	"github.com/tmargolis/context-memory-fabric/internal/features"
	"github.com/tmargolis/context-memory-fabric/internal/home"
	"github.com/tmargolis/context-memory-fabric/internal/probe"
	"github.com/tmargolis/context-memory-fabric/internal/remote"
	"github.com/tmargolis/context-memory-fabric/internal/run"
)

// The page: plain HTML/JS/CSS, no build step, so it can move into a native
// window later (plan section 3.1, option B) unchanged.
//
//go:embed web
var webFiles embed.FS

const tokenCookie = "cmf_ui"

// Replaced in tests.
var (
	uiIdleLimit = 30 * time.Minute
	uiTick      = time.Second
)

// Replaced in tests.
var openBrowser = func(url string) error {
	switch goos {
	case "darwin":
		return run.Exec{}.Run(context.Background(), run.Cmd{Name: "open", Args: []string{url}}).Err
	case "windows":
		return run.Exec{}.Run(context.Background(), run.Cmd{Name: "rundll32", Args: []string{"url.dll,FileProtocolHandler", url}}).Err
	}
	return run.Exec{}.Run(context.Background(), run.Cmd{Name: "xdg-open", Args: []string{url}}).Err
}

// uiServer is CMF management's local web UI (item 3.6): the same engine as the
// CLI, reachable only from this machine and only with the URL's token.
type uiServer struct {
	token string
	host  string // 127.0.0.1:<port>, the only Host header accepted

	mu       sync.Mutex
	busy     bool // one fix / apply / restore at a time
	lastSeen time.Time
	alive    int    // open /api/alive connections: one per open tab
	reason   string // why the server stopped, printed on exit
	done     chan struct{}
	stopOnce sync.Once

	release   *releaseInfo // the last update check, reused for releaseTTL
	checkedAt time.Time
	updatedTo string // set once Update replaced this cmf: the page asks for a restart
}

const releaseTTL = 10 * time.Minute

func newToken() string {
	b := make([]byte, 32)
	rand.Read(b)
	return hex.EncodeToString(b)
}

func runUI(args []string, out io.Writer) error {
	noOpen := false
	for _, a := range args {
		switch a {
		case "--no-open":
			noOpen = true
		default:
			return errors.New("usage: cmf ui [--no-open]")
		}
	}
	ln, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		return err
	}
	s := &uiServer{token: newToken(), host: ln.Addr().String(), lastSeen: time.Now(), done: make(chan struct{})}
	srv := &http.Server{Handler: s.routes(), ReadHeaderTimeout: 10 * time.Second}
	url := fmt.Sprintf("http://%s/?t=%s", s.host, s.token)
	fmt.Fprintln(out, "CMF management is running at:")
	fmt.Fprintln(out, "  "+url)
	fmt.Fprintln(out, "It stops with the Quit button, 30 minutes after its last tab closes, or with Ctrl-C.")
	if !noOpen {
		if err := openBrowser(url); err != nil {
			fmt.Fprintln(out, "Couldn't open a browser (", err, "); open the address above yourself.")
		}
	}
	go srv.Serve(ln)
	go s.watch()
	sig := make(chan os.Signal, 1)
	signal.Notify(sig, os.Interrupt)
	select {
	case <-s.done:
	case <-sig:
		s.stop("Ctrl-C")
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	srv.Shutdown(ctx)
	s.mu.Lock()
	reason := s.reason
	s.mu.Unlock()
	fmt.Fprintf(out, "CMF management stopped: %s.\n", reason)
	return nil
}

func (s *uiServer) stop(reason string) {
	s.stopOnce.Do(func() {
		s.mu.Lock()
		s.reason = reason
		s.mu.Unlock()
		close(s.done)
	})
}

// watch stops the server after the idle limit. An open tab counts as
// activity, and the idle clock starts when the last one goes away, so a tab
// the browser discards or freezes (Chrome Memory Saver, tab-discard
// extensions, sleep) gets the full limit to come back. A running fix or
// apply keeps it alive.
func (s *uiServer) watch() {
	for {
		select {
		case <-s.done:
			return
		case <-time.After(uiTick):
		}
		s.mu.Lock()
		now, busy := time.Now(), s.busy
		idle := s.alive == 0 && now.Sub(s.lastSeen) > uiIdleLimit
		s.mu.Unlock()
		if !busy && idle {
			s.stop(fmt.Sprintf("no open tab or request for %s", uiIdleLimit))
			return
		}
	}
}

func (s *uiServer) routes() http.Handler {
	mux := http.NewServeMux()
	static, _ := fs.Sub(webFiles, "web")
	files := http.FileServer(http.FS(static))
	mux.HandleFunc("GET /{$}", s.index)
	mux.Handle("GET /app.js", files)
	mux.Handle("GET /style.css", files)
	mux.HandleFunc("GET /api/status", s.api(s.status))
	mux.HandleFunc("GET /api/features", s.api(s.features))
	mux.HandleFunc("POST /api/plan", s.api(s.plan))
	mux.HandleFunc("POST /api/apply", s.stream(s.apply))
	mux.HandleFunc("POST /api/fix/{id}", s.stream(s.fix))
	mux.HandleFunc("GET /api/logs", s.api(s.logs))
	mux.HandleFunc("GET /api/diagnostics", s.api(s.diagnostics))
	mux.HandleFunc("GET /api/backups", s.api(s.backups))
	mux.HandleFunc("POST /api/restore/{stamp}", s.stream(s.restore))
	mux.HandleFunc("POST /api/instructions/{app}", s.stream(s.addInstructions))
	mux.HandleFunc("GET /api/update", s.api(s.updateCheck))
	mux.HandleFunc("POST /api/update", s.stream(s.update))
	mux.HandleFunc("POST /api/quit", s.quit)
	mux.HandleFunc("GET /api/alive", s.aliveHandler)
	return s.guard(mux)
}

// guard: only this machine, only the right Host (against DNS rebinding),
// only same-origin writes, and the token on everything but the opening URL.
func (s *uiServer) guard(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Cache-Control", "no-store")
		w.Header().Set("X-Frame-Options", "DENY")
		w.Header().Set("Referrer-Policy", "no-referrer")
		w.Header().Set("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'")
		if r.Host != s.host {
			http.Error(w, "wrong host", http.StatusForbidden)
			return
		}
		if r.Method != http.MethodGet {
			if o := r.Header.Get("Origin"); o != "" && o != "http://"+s.host {
				http.Error(w, "wrong origin", http.StatusForbidden)
				return
			}
		}
		if r.URL.Path == "/" && r.URL.Query().Get("t") != "" {
			next.ServeHTTP(w, r) // index checks the token itself and sets the cookie
			return
		}
		c, err := r.Cookie(tokenCookie)
		if err != nil || subtle.ConstantTimeCompare([]byte(c.Value), []byte(s.token)) != 1 {
			http.Error(w, "open CMF management from the address cmf printed (it carries a one-time token)", http.StatusUnauthorized)
			return
		}
		s.mu.Lock()
		s.lastSeen = time.Now()
		s.mu.Unlock()
		next.ServeHTTP(w, r)
	})
}

func (s *uiServer) index(w http.ResponseWriter, r *http.Request) {
	if t := r.URL.Query().Get("t"); t != "" {
		if subtle.ConstantTimeCompare([]byte(t), []byte(s.token)) != 1 {
			http.Error(w, "wrong token", http.StatusUnauthorized)
			return
		}
		http.SetCookie(w, &http.Cookie{Name: tokenCookie, Value: s.token, Path: "/", HttpOnly: true, SameSite: http.SameSiteStrictMode})
		http.Redirect(w, r, "/", http.StatusSeeOther) // the token leaves the address bar and history
		return
	}
	page, _ := webFiles.ReadFile("web/index.html")
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Write(page)
}

// quit is the page's Quit button. It waits for a running change to finish.
func (s *uiServer) quit(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	busy := s.busy
	s.mu.Unlock()
	if busy {
		http.Error(w, "a change is still running; quit when it finishes", http.StatusConflict)
		return
	}
	w.WriteHeader(http.StatusNoContent)
	s.stop("Quit button")
}

// aliveHandler holds one request open per tab. While any is open the server
// isn't idle; when the last one drops (the tab closed, was discarded, or the
// machine slept), the idle clock starts from that moment.
func (s *uiServer) aliveHandler(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	s.alive++
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		s.alive--
		s.lastSeen = time.Now()
		s.mu.Unlock()
	}()
	w.Header().Set("Content-Type", "text/plain")
	w.WriteHeader(http.StatusOK)
	if f, ok := w.(http.Flusher); ok {
		f.Flush()
	}
	select {
	case <-r.Context().Done():
	case <-s.done:
	}
}

func (s *uiServer) api(h func(*http.Request) (any, error)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		v, err := h(r)
		w.Header().Set("Content-Type", "application/json")
		if err != nil {
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(map[string]string{"error": err.Error()})
			return
		}
		json.NewEncoder(w).Encode(v)
	}
}

// lineWriter flushes each write, so the page shows progress as it happens.
type lineWriter struct {
	w http.ResponseWriter
	f http.Flusher
}

func (l lineWriter) Write(p []byte) (int, error) {
	n, err := l.w.Write(p)
	if l.f != nil {
		l.f.Flush()
	}
	return n, err
}

// stream runs a long action, sending its log as plain text and a last line
// "@@result {json}" with ok and, when it made one, the backup stamp.
func (s *uiServer) stream(h func(*http.Request, io.Writer) (string, error)) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		s.mu.Lock()
		if s.busy {
			s.mu.Unlock()
			http.Error(w, "another change is still running", http.StatusConflict)
			return
		}
		s.busy = true
		s.mu.Unlock()
		defer func() {
			s.mu.Lock()
			s.busy, s.lastSeen = false, time.Now()
			s.mu.Unlock()
		}()
		w.Header().Set("Content-Type", "text/plain; charset=utf-8")
		w.Header().Set("X-Content-Type-Options", "nosniff")
		f, _ := w.(http.Flusher)
		out := lineWriter{w, f}
		stamp, err := h(r, out)
		result := map[string]any{"ok": err == nil, "stamp": stamp}
		if err != nil {
			result["error"] = err.Error()
		}
		data, _ := json.Marshal(result)
		fmt.Fprintf(out, "\n@@result %s\n", data)
	}
}

func (s *uiServer) status(r *http.Request) (any, error) {
	facts, err := collect(context.Background())
	if err != nil {
		return nil, err
	}
	results := checks.Evaluate(facts)
	h, _ := home.Default()
	browser := 3001
	if env, err := loadEnv(h); err == nil {
		if p, err := strconv.Atoi(env.Get("CMF_BROWSER_PORT")); err == nil && p > 0 {
			browser = p
		}
	}
	graph, serverVersion := "", ""
	if facts.Health != nil {
		graph, serverVersion = facts.Health.Graph, facts.Health.Version
	}
	// The address you use for the Browser: Tailscale Serve's https one when
	// it publishes the Browser's port, this computer's otherwise.
	browserURL := fmt.Sprintf("http://127.0.0.1:%d", browser)
	if served := remote.ServedURL(r.Context(), runner, findProgram, browser); served != "" {
		browserURL = served
	}
	return map[string]any{"overall": checks.Worst(results), "checked_at": facts.At, "checks": results,
		"graph": graph, "nodes": facts.Nodes, "browser_url": browserURL, "version": version,
		"server_version": serverVersion, "versions_match": serverVersion == "" || checks.BaseVersion(serverVersion) == checks.BaseVersion(version)}, nil
}

// connectorSeen: Claude Desktop called CMF in the last day with no local
// entry, so a claude.ai connector reaches it (it covers every Claude app
// signed in to that account).
// modelsFrom is the models the server runs, for the LLM and Embeddings
// cards (v0.1.9); nil when the server can't be reached or predates it.
func modelsFrom(h *probe.Health) map[string]any {
	if h == nil || (len(h.Providers.LLMModels) == 0 && h.Providers.EmbedModel == "") {
		return nil
	}
	return map[string]any{"llm": h.Providers.LLMModels, "embeddings": h.Providers.EmbedModel}
}

// instructionsURL is the instruction block's doc, for the Features cards.
const instructionsURL = "https://github.com/tmargolis/context-memory-fabric/blob/main/docs/INSTRUCTIONS-FOR-AGENTS.md"

// serverHealth is the local server's /health, or nil.
func serverHealth(ctx context.Context, env *home.Env) *probe.Health {
	port := 8000
	if p, err := strconv.Atoi(env.Get("CMF_MCP_PORT")); err == nil && p > 0 {
		port = p
	}
	body, err := getHealth(ctx, fmt.Sprintf("http://127.0.0.1:%d/health", port))
	if err != nil {
		return nil
	}
	var h probe.Health
	if json.Unmarshal(body, &h) != nil {
		return nil
	}
	return &h
}

func connectorSeen(h *probe.Health) bool {
	if h == nil {
		return false
	}
	for _, c := range h.ClientsSeen {
		if c.Harness == "claude_desktop" {
			return true
		}
	}
	return false
}

func (s *uiServer) features(r *http.Request) (any, error) {
	h, err := home.Default()
	if err != nil {
		return nil, err
	}
	m, err := features.Load()
	if err != nil {
		return nil, err
	}
	env, err := loadEnv(h)
	if err != nil {
		return nil, err
	}
	user, _ := userHome()
	health := serverHealth(r.Context(), env)
	server := ""
	if health != nil {
		server = health.Version
	}
	cenv := clientEnv()
	var live map[string]probe.LiveCapture
	if health != nil {
		live = health.LiveCaptures
	}
	states := capture.Report(user, env, now())
	for i := range states {
		capture.DescribeHook(&states[i], server, contextmemoryfabric.PluginVersion())
	}
	return map[string]any{"groups": m.Groups, "features": m.States(env),
		"clients": clients.InspectAll(clientEnv(), clientTarget(env)),
		// Each transcript source with its checkpoint hook, for the AI app
		// cards, and the public address a claude.ai connector would use.
		"capture": states, "public_url": env.Get("CMF_MCP_ISSUER_URL"),
		"connector":    connectorSeen(health),
		"instructions": contextmemoryfabric.AgentInstructions(), "instructions_doc": instructionsURL,
		// Whether each app's instructions file holds CMF's guidance, and what
		// each app captured lately (for apps whose instructions CMF can't read).
		"instructions_state": capture.CheckInstructions(cenv.Home, cenv.Getenv), "live_captures": live,
		"models": modelsFrom(health)}, nil
}

func readRequest(r *http.Request) (*features.Request, error) {
	data, err := io.ReadAll(io.LimitReader(r.Body, 1<<20))
	if err != nil {
		return nil, err
	}
	return features.ParseRequest(data)
}

type planView struct {
	OK       bool     `json:"ok"`
	Empty    bool     `json:"empty"`
	Review   []string `json:"review"`
	Errors   []string `json:"errors"`
	Warnings []string `json:"warnings"`
	Blocked  string   `json:"blocked,omitempty"`
}

func (s *uiServer) plan(r *http.Request) (any, error) {
	req, err := readRequest(r)
	if err != nil {
		return nil, err
	}
	rp, err := planRequest(r.Context(), req)
	if err != nil {
		return nil, err
	}
	v := planView{OK: rp.Plan.OK(), Empty: rp.Empty(), Errors: rp.Plan.Errors, Warnings: rp.Plan.Warnings}
	if v.OK && !v.Empty {
		v.Review = rp.Review()
		if err := rp.Ready(); err != nil {
			v.Blocked = err.Error()
		}
	}
	return v, nil
}

func (s *uiServer) apply(r *http.Request, out io.Writer) (string, error) {
	req, err := readRequest(r)
	if err != nil {
		return "", err
	}
	// Planned again here, so what runs is what the machine is now.
	rp, err := planRequest(r.Context(), req)
	if err != nil {
		return "", err
	}
	if !rp.Plan.OK() {
		return "", errors.New(strings.Join(rp.Plan.Errors, "; "))
	}
	if rp.Empty() {
		fmt.Fprintln(out, "Nothing to change.")
		return "", nil
	}
	if err := rp.Ready(); err != nil {
		return "", err
	}
	backup, err := rp.Apply(r.Context(), out)
	if backup != nil {
		return backup.Stamp, err
	}
	return "", err
}

func (s *uiServer) fix(r *http.Request, out io.Writer) (string, error) {
	id := r.PathValue("id")
	ctx := r.Context()
	facts, err := collect(ctx)
	if err != nil {
		return "", err
	}
	for _, res := range checks.Evaluate(facts) {
		if res.ID != id {
			continue
		}
		fix := res.Fix
		if fix == nil || res.Level == checks.OK {
			fix = res.Action // run-any-time actions, such as the self-test
		}
		if fix == nil {
			return "", fmt.Errorf("nothing to fix for %q", id)
		}
		fmt.Fprintf(out, "Running: %s\n", fix.Label)
		if err := checks.ApplyFix(ctx, runner, fix, func(s string) { fmt.Fprintln(out, s) }); err != nil {
			return "", err
		}
		if id == "docker" {
			waitForDocker(ctx, out, facts)
		}
		fmt.Fprintln(out, "Done.")
		return "", nil
	}
	return "", fmt.Errorf("no check %q", id)
}

func (s *uiServer) logs(r *http.Request) (any, error) {
	h, err := home.Default()
	if err != nil {
		return nil, err
	}
	tail := 300
	if n, err := strconv.Atoi(r.URL.Query().Get("tail")); err == nil && n > 0 && n <= 5000 {
		tail = n
	}
	var services []string
	if svc := r.URL.Query().Get("service"); svc != "" {
		services = append(services, svc)
	}
	res := runner.Run(r.Context(), composeLogs(h, tail, services...))
	if res.Err != nil {
		return nil, fmt.Errorf("docker compose logs: %s", firstLine(res.Output(), res.Err))
	}
	// Which lines are errors, by the rule diagnostics uses, so the viewer
	// marks and filters exactly what a report would carry.
	errLines := []int{}
	for i, line := range strings.Split(res.Stdout, "\n") {
		if diag.IsError(line) {
			errLines = append(errLines, i)
		}
	}
	return map[string]any{"text": res.Stdout, "errors": errLines}, nil
}

func (s *uiServer) diagnostics(r *http.Request) (any, error) {
	report, err := buildReport(r.Context())
	if err != nil {
		return nil, err
	}
	return map[string]string{"report": report}, nil
}

func (s *uiServer) backups(*http.Request) (any, error) {
	h, err := home.Default()
	if err != nil {
		return nil, err
	}
	list, err := h.Backups()
	if err != nil {
		return nil, err
	}
	return list, nil
}

// addInstructions writes CMF's instruction block into an app's file (the
// Features cards' Add button), the same as `cmf instructions add <app>`.
// updateCheck answers the Update button: is there a newer release? ?fresh=1 skips the cache.
func (s *uiServer) updateCheck(r *http.Request) (any, error) {
	s.mu.Lock()
	info, at, updated := s.release, s.checkedAt, s.updatedTo
	s.mu.Unlock()
	if updated != "" {
		return map[string]any{"current": version, "latest": updated, "restart": true}, nil
	}
	if info == nil || r.URL.Query().Get("fresh") != "" || time.Since(at) > releaseTTL {
		fresh, err := latestRelease(r.Context())
		if err != nil {
			return nil, err
		}
		s.mu.Lock()
		s.release, s.checkedAt, info = fresh, time.Now(), fresh
		s.mu.Unlock()
	}
	return info, nil
}

func (s *uiServer) update(r *http.Request, out io.Writer) (string, error) {
	installed, err := installLatest(r.Context(), false, true, out)
	if installed != "" {
		s.mu.Lock()
		s.updatedTo = installed
		s.mu.Unlock()
	}
	return "", err
}

func (s *uiServer) addInstructions(r *http.Request, out io.Writer) (string, error) {
	return "", runInstructions([]string{"add", r.PathValue("app"), "--yes"}, out)
}

func (s *uiServer) restore(r *http.Request, out io.Writer) (string, error) {
	h, err := home.Default()
	if err != nil {
		return "", err
	}
	m, err := features.Load()
	if err != nil {
		return "", err
	}
	facts, err := collect(r.Context())
	if err != nil {
		return "", err
	}
	undo, err := executor(h, m, facts.Port, out).Restore(r.Context(), r.PathValue("stamp"))
	if undo != nil {
		return undo.Stamp, err
	}
	return "", err
}
