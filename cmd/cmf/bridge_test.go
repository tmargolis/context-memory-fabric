package main

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
)

// fakeMCP answers like the server's streamable-HTTP endpoint: initialize as
// JSON with a session id, tools/list as SSE, notifications with 202.
type fakeMCP struct {
	mu      sync.Mutex
	seen    []http.Header
	deleted bool
}

func (f *fakeMCP) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	f.mu.Lock()
	f.seen = append(f.seen, r.Header.Clone())
	f.mu.Unlock()
	if r.Method == http.MethodDelete {
		f.mu.Lock()
		f.deleted = true
		f.mu.Unlock()
		return
	}
	body, _ := io.ReadAll(r.Body)
	var m struct {
		ID     json.RawMessage
		Method string
	}
	json.Unmarshal(body, &m)
	switch m.Method {
	case "initialize":
		w.Header().Set("Mcp-Session-Id", "sess-1")
		w.Header().Set("Content-Type", "application/json")
		w.Write([]byte(`{"jsonrpc":"2.0","id":` + string(m.ID) + `,"result":{"protocolVersion":"2025-06-18","capabilities":{}}}`))
	case "notifications/initialized":
		w.WriteHeader(http.StatusAccepted)
	default:
		w.Header().Set("Content-Type", "text/event-stream")
		w.Write([]byte("event: message\ndata: {\"jsonrpc\":\"2.0\",\"method\":\"notifications/progress\"}\n\nevent: message\ndata: {\"jsonrpc\":\"2.0\",\"id\":" + string(m.ID) + ",\ndata:\"result\":{\"tools\":[]}}\n\n"))
	}
}

func TestBridgeRelaysJSONAndSSE(t *testing.T) {
	f := &fakeMCP{}
	srv := httptest.NewServer(f)
	defer srv.Close()
	var out, log bytes.Buffer
	b := &bridge{url: srv.URL + "/mcp", token: "tok", client: srv.Client(), out: &out, log: &log}
	in := strings.Join([]string{
		`{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}`,
		`{"jsonrpc":"2.0","method":"notifications/initialized"}`,
		`{"jsonrpc":"2.0","id":2,"method":"tools/list"}`,
	}, "\n") + "\n"
	if err := b.run(context.Background(), strings.NewReader(in)); err != nil {
		t.Fatal(err)
	}
	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	if len(lines) != 3 || !strings.Contains(lines[0], `"protocolVersion":"2025-06-18"`) || !strings.Contains(out.String(), `"id":2,`) {
		t.Fatalf("%q", out.String())
	}
	for _, l := range lines {
		if !json.Valid([]byte(l)) {
			t.Fatalf("not one JSON message per line: %q", l)
		}
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	last := f.seen[len(f.seen)-1]
	if last.Get("Mcp-Session-Id") != "sess-1" || last.Get("MCP-Protocol-Version") != "2025-06-18" || last.Get("Authorization") != "Bearer tok" || !f.deleted {
		t.Fatalf("%v deleted=%v", last, f.deleted)
	}
}

func TestBridgeServerDownAnswersEachRequest(t *testing.T) {
	var out, log bytes.Buffer
	b := &bridge{url: "http://127.0.0.1:1/mcp", client: &http.Client{}, out: &out, log: &log}
	in := `{"jsonrpc":"2.0","id":7,"method":"tools/list"}` + "\n" + `{"jsonrpc":"2.0","method":"notifications/initialized"}` + "\n"
	b.run(context.Background(), strings.NewReader(in))
	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	if len(lines) != 1 || !strings.Contains(lines[0], `"id":7`) || !strings.Contains(lines[0], "cmf status") {
		t.Fatalf("%q", out.String()) // the notification gets no answer
	}
}

func TestBridgeUnauthorized(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { w.WriteHeader(http.StatusUnauthorized) }))
	defer srv.Close()
	var out, log bytes.Buffer
	b := &bridge{url: srv.URL, client: srv.Client(), out: &out, log: &log}
	b.run(context.Background(), strings.NewReader(`{"jsonrpc":"2.0","id":1,"method":"initialize"}`+"\n"))
	if !strings.Contains(out.String(), "CMF_MCP_AUTH_TOKEN") {
		t.Fatal(out.String())
	}
}
