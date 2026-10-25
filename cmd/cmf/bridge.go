package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/home"
)

// runBridge relays an AI app's stdio MCP to the CMF server's streamable-HTTP
// endpoint (item 3.8), so Claude Desktop runs this binary instead of
// `npx mcp-remote` and needs no Node. Port and bearer token come from
// ~/.cmf/.env, so the app's config holds no secret. stdout carries only
// JSON-RPC; everything else goes to stderr.
func runBridge(args []string, _ io.Writer) error {
	url := ""
	for i := 0; i < len(args); i++ {
		switch a := args[i]; {
		case a == "--url" && i+1 < len(args):
			i++
			url = args[i]
		case strings.HasPrefix(a, "--url="):
			url = strings.TrimPrefix(a, "--url=")
		default:
			return errors.New("usage: cmf bridge [--url http://127.0.0.1:8000/mcp]")
		}
	}
	token := os.Getenv("CMF_MCP_AUTH_TOKEN")
	if h, err := home.Default(); err == nil {
		if env, err := loadEnv(h); err == nil {
			t := clientTarget(env)
			if url == "" {
				url = t.LocalURL()
			}
			if token == "" {
				token = t.Token
			}
		}
	}
	if url == "" {
		url = "http://127.0.0.1:8000/mcp"
	}
	b := &bridge{url: url, token: token, client: &http.Client{}, out: bridgeStdout, log: bridgeStderr}
	return b.run(context.Background(), bridgeStdin)
}

// Replaced in tests.
var (
	bridgeStdin  io.Reader = os.Stdin
	bridgeStdout io.Writer = os.Stdout
	bridgeStderr io.Writer = os.Stderr
)

type bridge struct {
	url, token string
	client     *http.Client
	out, log   io.Writer

	outMu   sync.Mutex
	mu      sync.Mutex
	session string
	version string // MCP-Protocol-Version, once initialize answered
}

type rpcMessage struct {
	ID     json.RawMessage `json:"id,omitempty"`
	Method string          `json:"method,omitempty"`
	Result json.RawMessage `json:"result,omitempty"`
}

func (b *bridge) emit(msg []byte) {
	msg = bytes.TrimSpace(msg)
	if len(msg) == 0 {
		return
	}
	var compact bytes.Buffer
	if json.Compact(&compact, msg) == nil {
		msg = compact.Bytes()
	}
	b.outMu.Lock()
	defer b.outMu.Unlock()
	b.out.Write(append(msg, '\n'))
}

func (b *bridge) logf(format string, a ...any) { fmt.Fprintf(b.log, "cmf bridge: "+format+"\n", a...) }

// fail answers a request the server couldn't, so the app shows why instead of waiting.
func (b *bridge) fail(id json.RawMessage, msg string) {
	if len(id) == 0 || string(id) == "null" {
		return // a notification or response: nothing to answer
	}
	resp, _ := json.Marshal(map[string]any{"jsonrpc": "2.0", "id": id, "error": map[string]any{"code": -32000, "message": msg}})
	b.emit(resp)
}

func (b *bridge) run(ctx context.Context, in io.Reader) error {
	scanner := bufio.NewScanner(in)
	scanner.Buffer(make([]byte, 64*1024), 16*1024*1024)
	var wg sync.WaitGroup
	for scanner.Scan() {
		line := bytes.TrimSpace(scanner.Bytes())
		if len(line) == 0 {
			continue
		}
		msg := append([]byte(nil), line...)
		var m rpcMessage
		json.Unmarshal(msg, &m)
		if m.Method == "initialize" {
			wg.Wait() // nothing else may race the session it creates
			b.send(ctx, msg, m)
			continue
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			b.send(ctx, msg, m)
		}()
	}
	wg.Wait()
	b.close(ctx)
	return scanner.Err()
}

func (b *bridge) send(ctx context.Context, msg []byte, m rpcMessage) {
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, b.url, bytes.NewReader(msg))
	if err != nil {
		b.fail(m.ID, err.Error())
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json, text/event-stream")
	b.headers(req)
	resp, err := b.client.Do(req)
	if err != nil {
		b.logf("%v", err)
		b.fail(m.ID, fmt.Sprintf("The CMF server isn't answering at %s. Run `cmf status` (or open CMF management) to see why.", b.url))
		return
	}
	defer resp.Body.Close()
	if sid := resp.Header.Get("Mcp-Session-Id"); sid != "" {
		b.mu.Lock()
		b.session = sid
		b.mu.Unlock()
	}
	switch {
	case resp.StatusCode == http.StatusAccepted:
		return
	case resp.StatusCode == http.StatusUnauthorized:
		b.fail(m.ID, "The CMF server refused the bridge (401). Set CMF_MCP_AUTH_TOKEN in ~/.cmf/.env to the server's token; a server using OAuth (CMF_MCP_ISSUER_URL) needs a connector instead of the bridge.")
		return
	case resp.StatusCode == http.StatusNotFound && b.sessionID() != "":
		b.fail(m.ID, "The CMF server restarted and forgot this session. Start a new chat (or restart the app) to reconnect.")
		return
	case resp.StatusCode >= 400:
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 4096))
		b.fail(m.ID, fmt.Sprintf("The CMF server answered HTTP %d: %s", resp.StatusCode, strings.TrimSpace(string(body))))
		return
	}
	if strings.HasPrefix(resp.Header.Get("Content-Type"), "text/event-stream") {
		b.readEvents(resp.Body, m)
		return
	}
	body, _ := io.ReadAll(resp.Body)
	b.note(body, m)
	b.emit(body)
}

func (b *bridge) headers(req *http.Request) {
	b.mu.Lock()
	defer b.mu.Unlock()
	if b.session != "" {
		req.Header.Set("Mcp-Session-Id", b.session)
	}
	if b.version != "" {
		req.Header.Set("MCP-Protocol-Version", b.version)
	}
	if b.token != "" {
		req.Header.Set("Authorization", "Bearer "+b.token)
	}
}

func (b *bridge) sessionID() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.session
}

// note keeps the protocol version initialize agreed on, for later requests.
func (b *bridge) note(body []byte, m rpcMessage) {
	if m.Method != "initialize" {
		return
	}
	var r struct {
		Result struct {
			ProtocolVersion string `json:"protocolVersion"`
		} `json:"result"`
	}
	if json.Unmarshal(body, &r) == nil && r.Result.ProtocolVersion != "" {
		b.mu.Lock()
		b.version = r.Result.ProtocolVersion
		b.mu.Unlock()
	}
}

// readEvents relays each SSE event's data (one JSON-RPC message per event).
func (b *bridge) readEvents(body io.Reader, m rpcMessage) {
	scanner := bufio.NewScanner(body)
	scanner.Buffer(make([]byte, 64*1024), 16*1024*1024)
	var data []string
	flush := func() {
		if len(data) > 0 {
			msg := []byte(strings.Join(data, "\n"))
			b.note(msg, m)
			b.emit(msg)
			data = nil
		}
	}
	for scanner.Scan() {
		line := scanner.Text()
		switch {
		case line == "":
			flush()
		case strings.HasPrefix(line, "data:"):
			data = append(data, strings.TrimPrefix(strings.TrimPrefix(line, "data:"), " "))
		}
	}
	flush()
}

// close ends the session on the server when the app goes away.
func (b *bridge) close(ctx context.Context) {
	sid := b.sessionID()
	if sid == "" {
		return
	}
	ctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodDelete, b.url, nil)
	if err != nil {
		return
	}
	b.headers(req)
	if resp, err := b.client.Do(req); err == nil {
		resp.Body.Close()
	}
}
