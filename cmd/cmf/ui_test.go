package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/run"
)

func uiFixture(t *testing.T) (*uiServer, http.Handler, *run.Fake) {
	fake := fakeMachine(t, true)
	s := &uiServer{token: "tok123", host: "127.0.0.1:5555", lastSeen: time.Now(), done: make(chan struct{})}
	return s, s.routes(), fake
}

func call(h http.Handler, method, path, body string, mod func(*http.Request)) *httptest.ResponseRecorder {
	r := httptest.NewRequest(method, "http://127.0.0.1:5555"+path, strings.NewReader(body))
	r.Host = "127.0.0.1:5555"
	r.AddCookie(&http.Cookie{Name: tokenCookie, Value: "tok123"})
	if mod != nil {
		mod(r)
	}
	w := httptest.NewRecorder()
	h.ServeHTTP(w, r)
	return w
}

func TestUITokenHostAndOrigin(t *testing.T) {
	_, h, _ := uiFixture(t)
	noCookie := func(r *http.Request) { r.Header.Del("Cookie") }
	if w := call(h, "GET", "/api/status", "", noCookie); w.Code != http.StatusUnauthorized {
		t.Fatal("no token", w.Code)
	}
	if w := call(h, "GET", "/api/status", "", func(r *http.Request) {
		r.Header.Del("Cookie")
		r.AddCookie(&http.Cookie{Name: tokenCookie, Value: "nope"})
	}); w.Code != http.StatusUnauthorized {
		t.Fatal("wrong token", w.Code)
	}
	if w := call(h, "GET", "/api/status", "", func(r *http.Request) { r.Host = "evil.example:5555" }); w.Code != http.StatusForbidden {
		t.Fatal("DNS rebinding: wrong Host must be refused", w.Code)
	}
	if w := call(h, "POST", "/api/plan", `{"clients":{"cursor":true}}`, func(r *http.Request) { r.Header.Set("Origin", "http://evil.example") }); w.Code != http.StatusForbidden {
		t.Fatal("cross-origin POST", w.Code)
	}
	// The opening URL trades the token for a cookie and drops it from the address.
	w := call(h, "GET", "/?t=tok123", "", noCookie)
	if w.Code != http.StatusSeeOther || w.Header().Get("Location") != "/" {
		t.Fatal(w.Code, w.Header())
	}
	c := w.Result().Cookies()
	if len(c) != 1 || c[0].Value != "tok123" || !c[0].HttpOnly || c[0].SameSite != http.SameSiteStrictMode {
		t.Fatalf("%+v", c)
	}
	if w := call(h, "GET", "/?t=wrong", "", noCookie); w.Code != http.StatusUnauthorized {
		t.Fatal("wrong opening token", w.Code)
	}
	if w := call(h, "GET", "/", "", nil); w.Code != 200 || !strings.Contains(w.Body.String(), "<title>CMF Setup</title>") || !strings.Contains(w.Header().Get("Content-Security-Policy"), "script-src 'self'") {
		t.Fatal(w.Code, w.Body.String()[:80])
	}
	if w := call(h, "GET", "/app.js", "", nil); w.Code != 200 || !strings.Contains(w.Body.String(), "textContent") {
		t.Fatal("app.js", w.Code)
	}
}

func TestUIStatusFeaturesAndPlan(t *testing.T) {
	_, h, _ := uiFixture(t)
	w := call(h, "GET", "/api/status", "", nil)
	var status struct {
		Overall string
		Checks  []struct{ ID, Level string }
	}
	if json.Unmarshal(w.Body.Bytes(), &status); w.Code != 200 || len(status.Checks) < 10 {
		t.Fatal(w.Code, w.Body.String())
	}
	w = call(h, "GET", "/api/features", "", nil)
	var f struct {
		Groups   []string
		Features []struct{ ID string }
		Clients  []struct{ ID string }
	}
	if json.Unmarshal(w.Body.Bytes(), &f); len(f.Features) == 0 || len(f.Clients) != 6 {
		t.Fatal(w.Body.String())
	}
	os.MkdirAll(filepath.Join(clientEnv().Home, ".cursor"), 0o700)
	w = call(h, "POST", "/api/plan", `{"clients":{"cursor":true}}`, func(r *http.Request) { r.Header.Set("Origin", "http://127.0.0.1:5555") })
	var plan planView
	if json.Unmarshal(w.Body.Bytes(), &plan); !plan.OK || plan.Empty || !strings.Contains(strings.Join(plan.Review, "\n"), `add "context-memory-fabric"`) {
		t.Fatal(w.Body.String())
	}
	w = call(h, "POST", "/api/plan", `{"features":{"nope":{"enabled":true}}}`, nil)
	if json.Unmarshal(w.Body.Bytes(), &plan); plan.OK || !strings.Contains(strings.Join(plan.Errors, ""), `unknown feature "nope"`) {
		t.Fatal(w.Body.String())
	}
}

func TestUIApplyStreamsAndLocks(t *testing.T) {
	s, h, _ := uiFixture(t)
	os.MkdirAll(filepath.Join(clientEnv().Home, ".cursor"), 0o700)
	w := call(h, "POST", "/api/apply", `{"clients":{"cursor":true}}`, nil)
	body := w.Body.String()
	if !strings.Contains(body, "Cursor: Reload Cursor.") || !strings.Contains(body, `@@result {"ok":true,"stamp":"`) {
		t.Fatal(body)
	}
	s.busy = true
	if w := call(h, "POST", "/api/apply", `{"clients":{"cursor":false}}`, nil); w.Code != http.StatusConflict {
		t.Fatal("a second change while one runs", w.Code)
	}
	s.busy = false
	w = call(h, "POST", "/api/fix/selftest", "", nil)
	if !strings.Contains(w.Body.String(), `"ok":false`) {
		t.Fatal(w.Body.String()) // no fix for a healthy or n/a row
	}
}

func TestUIDiagnosticsAndLogs(t *testing.T) {
	_, h, fake := uiFixture(t)
	fake.Answers["docker compose*"] = run.Result{Stdout: "cmf-server | ERROR boom\n"}
	w := call(h, "GET", "/api/diagnostics", "", nil)
	var d struct{ Report string }
	if json.Unmarshal(w.Body.Bytes(), &d); !strings.HasPrefix(d.Report, "CMF diagnostics") {
		t.Fatal(w.Body.String())
	}
	w = call(h, "GET", "/api/logs?service=cmf-server&tail=50", "", nil)
	if !strings.Contains(w.Body.String(), "ERROR boom") || !fake.Ran("docker compose --project-directory "+os.Getenv("CMF_HOME")+" logs --no-color --tail 50 cmf-server") {
		t.Fatal(w.Body.String())
	}
}

func TestUIStopsAfterTabCloses(t *testing.T) {
	s, h, _ := uiFixture(t)
	uiCloseGrace, uiTick = 20*time.Millisecond, 5*time.Millisecond
	t.Cleanup(func() { uiCloseGrace, uiTick = 15*time.Second, time.Second })
	go s.watch()
	call(h, "POST", "/api/bye", "", nil)
	select {
	case <-s.done:
	case <-time.After(2 * time.Second):
		t.Fatal("server didn't stop after the tab closed")
	}
}

func TestUIReloadKeepsItRunning(t *testing.T) {
	s, h, _ := uiFixture(t)
	uiCloseGrace, uiTick = 50*time.Millisecond, 5*time.Millisecond
	t.Cleanup(func() { uiCloseGrace, uiTick = 15*time.Second, time.Second })
	call(h, "POST", "/api/bye", "", nil)
	s.mu.Lock()
	s.byeAt = time.Now().Add(-2 * time.Second) // the bye came a moment before the reload
	s.mu.Unlock()
	call(h, "GET", "/", "", nil) // the reloaded page
	go s.watch()
	select {
	case <-s.done:
		t.Fatal("a reload must not stop the server")
	case <-time.After(200 * time.Millisecond):
	}
	s.stop()
}

func TestNoArgsOpensUI(t *testing.T) {
	fakeMachine(t, true)
	opened := ""
	openBrowser = func(url string) error { opened = url; return nil }
	t.Cleanup(func() { openBrowser = func(string) error { return nil } })
	var out bytes.Buffer
	done := make(chan int)
	go func() { done <- runCLI(nil, &out, io.Discard) }()
	deadline := time.Now().Add(3 * time.Second)
	for opened == "" && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	if !strings.HasPrefix(opened, "http://127.0.0.1:") || !strings.Contains(opened, "/?t=") {
		t.Fatal(opened)
	}
	// Close the tab: the server stops on its own.
	uiCloseGrace, uiTick = 10*time.Millisecond, 5*time.Millisecond
	t.Cleanup(func() { uiCloseGrace, uiTick = 15*time.Second, time.Second })
	host := strings.TrimPrefix(strings.Split(opened, "/?")[0], "http://")
	req, _ := http.NewRequest("POST", "http://"+host+"/api/bye", nil)
	req.AddCookie(&http.Cookie{Name: tokenCookie, Value: strings.Split(opened, "t=")[1]})
	if _, err := http.DefaultClient.Do(req); err != nil {
		t.Fatal(err)
	}
	select {
	case code := <-done:
		if code != 0 || !strings.Contains(out.String(), "CMF Setup stopped.") {
			t.Fatal(code, out.String())
		}
	case <-time.After(5 * time.Second):
		t.Fatal("UI didn't stop")
	}
}

func TestUIStopsWhenTabConnectionDrops(t *testing.T) {
	s, h, _ := uiFixture(t)
	uiCloseGrace, uiTick = 20*time.Millisecond, 5*time.Millisecond
	t.Cleanup(func() { uiCloseGrace, uiTick = 15*time.Second, time.Second })
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		r.Host = "127.0.0.1:5555"
		h.ServeHTTP(w, r)
	}))
	defer srv.Close()
	go s.watch()
	req, _ := http.NewRequest("GET", srv.URL+"/api/alive", nil)
	req.AddCookie(&http.Cookie{Name: tokenCookie, Value: "tok123"})
	ctx, cancel := context.WithCancel(context.Background())
	resp, err := http.DefaultClient.Do(req.WithContext(ctx))
	if err != nil {
		t.Fatal(err)
	}
	time.Sleep(60 * time.Millisecond)
	select {
	case <-s.done:
		t.Fatal("stopped while the tab was open")
	default:
	}
	cancel() // the tab closes without a "bye"
	resp.Body.Close()
	select {
	case <-s.done:
	case <-time.After(2 * time.Second):
		t.Fatal("didn't stop after the tab's connection dropped")
	}
}
