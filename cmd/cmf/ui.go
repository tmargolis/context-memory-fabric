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

	"github.com/tmargolis/context-memory-fabric/internal/checks"
	"github.com/tmargolis/context-memory-fabric/internal/clients"
	"github.com/tmargolis/context-memory-fabric/internal/features"
	"github.com/tmargolis/context-memory-fabric/internal/home"
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
	uiIdleLimit  = 30 * time.Minute
	uiCloseGrace = 15 * time.Second // a reload sends "bye" and comes straight back
	uiTick       = time.Second
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

// uiServer is CMF Setup's local web UI (item 3.6): the same engine as the
// CLI, reachable only from this machine and only with the URL's token.
type uiServer struct {
	token string
	host  string // 127.0.0.1:<port>, the only Host header accepted

	mu       sync.Mutex
	busy     bool // one fix / apply / restore at a time
	lastSeen time.Time
	byeAt    time.Time
	alive    int // open /api/alive connections: one per open tab
	done     chan struct{}
	stopOnce sync.Once
}

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
	fmt.Fprintln(out, "CMF Setup is running at:")
	fmt.Fprintln(out, "  "+url)
	fmt.Fprintln(out, "It stops when you close the tab, after 30 idle minutes, or with Ctrl-C.")
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
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	srv.Shutdown(ctx)
	fmt.Fprintln(out, "CMF Setup stopped.")
	return nil
}

func (s *uiServer) stop() { s.stopOnce.Do(func() { close(s.done) }) }

// watch stops the server after the tab closed (and didn't come back) or
// after the idle limit. A running fix or apply keeps it alive.
func (s *uiServer) watch() {
	for {
		select {
		case <-s.done:
			return
		case <-time.After(uiTick):
		}
		s.mu.Lock()
		now, busy := time.Now(), s.busy
		closed := s.alive == 0 && !s.byeAt.IsZero() && now.Sub(s.byeAt) > uiCloseGrace && s.lastSeen.Before(s.byeAt.Add(time.Second))
		idle := now.Sub(s.lastSeen) > uiIdleLimit
		s.mu.Unlock()
		if !busy && (closed || idle) {
			s.stop()
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
	mux.HandleFunc("POST /api/bye", s.bye)
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
			http.Error(w, "open CMF Setup from the address cmf printed (it carries a one-time token)", http.StatusUnauthorized)
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

func (s *uiServer) bye(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	s.byeAt = time.Now()
	s.mu.Unlock()
	w.WriteHeader(http.StatusNoContent)
}

// aliveHandler holds one request open per tab. When the last one drops (the
// tab closed, or the browser quit without a "bye"), the close grace starts.
func (s *uiServer) aliveHandler(w http.ResponseWriter, r *http.Request) {
	s.mu.Lock()
	s.alive++
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		if s.alive--; s.alive == 0 {
			s.byeAt = time.Now()
		}
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

func (s *uiServer) status(*http.Request) (any, error) {
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
	graph := ""
	if facts.Health != nil {
		graph = facts.Health.Graph
	}
	return map[string]any{"overall": checks.Worst(results), "checked_at": facts.At, "checks": results,
		"graph": graph, "nodes": facts.Nodes, "browser_url": fmt.Sprintf("http://127.0.0.1:%d", browser), "version": version}, nil
}

func (s *uiServer) features(*http.Request) (any, error) {
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
	return map[string]any{"groups": m.Groups, "features": m.States(env),
		"clients": clients.InspectAll(clientEnv(), clientTarget(env))}, nil
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
		if res.Fix == nil || res.Level == checks.OK {
			return "", fmt.Errorf("nothing to fix for %q", id)
		}
		fmt.Fprintf(out, "Fixing: %s\n", res.Fix.Label)
		if err := checks.ApplyFix(ctx, runner, res.Fix, func(s string) { fmt.Fprintln(out, s) }); err != nil {
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
	return map[string]string{"text": res.Stdout}, nil
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
