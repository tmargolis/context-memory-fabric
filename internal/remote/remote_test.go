package remote

import (
	"context"
	"strings"
	"testing"

	"github.com/tmargolis/context-memory-fabric/internal/home"
	"github.com/tmargolis/context-memory-fabric/internal/run"
)

const status = `{"BackendState":"Running","Self":{"DNSName":"mac.tail1.ts.net.","CapMap":{"funnel":null,"https":null}}}`

func detect(t *testing.T, funnel string) Tool {
	fake := &run.Fake{Answers: map[string]run.Result{
		"/usr/local/bin/tailscale status --json":        {Stdout: status},
		"/usr/local/bin/tailscale funnel status --json": {Stdout: funnel},
	}}
	find := func(name string) (string, bool) {
		if name == "tailscale" {
			return "/usr/local/bin/tailscale", true
		}
		if name == "cloudflared" {
			return "/opt/homebrew/bin/cloudflared", true
		}
		return "", false
	}
	tools := Detect(context.Background(), fake, find, 8000)
	if len(tools) != 3 || !tools[1].Installed || tools[2].Installed {
		t.Fatalf("%+v", tools)
	}
	return tools[0]
}

// Todd's machine: Funnel already publishes CMF on 443; 8443 serves the FalkorDB Browser (tailnet only).
const toddFunnel = `{"TCP":{"443":{"HTTPS":true},"8443":{"HTTPS":true}},"Web":{"mac.tail1.ts.net:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:8000"}}},"mac.tail1.ts.net:8443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:3001"}}}},"AllowFunnel":{"mac.tail1.ts.net:443":true}}`

func TestAlreadyPublishedNeedsNoCommand(t *testing.T) {
	ts := detect(t, toddFunnel)
	if !ts.Running || ts.Host != "mac.tail1.ts.net" || ts.ExposedURL != "https://mac.tail1.ts.net" {
		t.Fatalf("%+v", ts)
	}
	env := &home.Env{}
	env.Set("CMF_MCP_ISSUER_URL", "https://mac.tail1.ts.net")
	env.Set("CMF_MCP_OAUTH_PASSWORD", "existing")
	p, err := PlanTailscale(ts, env, 8000)
	if err != nil || len(p.Commands) != 0 || p.Record.SetIssuer || p.Record.SetPassword {
		t.Fatalf("%v %+v", err, p)
	}
}

func TestPicksAFreeFunnelPortAndTurnsOAuthOn(t *testing.T) {
	// 443 serves something else, 8443 too: CMF goes to 10000 and nothing else changes.
	ts := detect(t, `{"Web":{"mac.tail1.ts.net:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:9000"}}},"mac.tail1.ts.net:8443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:3001"}}}},"AllowFunnel":{"mac.tail1.ts.net:443":true}}`)
	if ts.ExposedURL != "" {
		t.Fatal("another service's Funnel isn't CMF's")
	}
	p, err := PlanTailscale(ts, &home.Env{}, 8000)
	if err != nil {
		t.Fatal(err)
	}
	if len(p.Commands) != 1 || p.Commands[0].String() != "/usr/local/bin/tailscale funnel --bg --https=10000 http://127.0.0.1:8000" {
		t.Fatal(p.Commands)
	}
	if p.Env.Get("CMF_MCP_ISSUER_URL") != "https://mac.tail1.ts.net:10000" || len(p.Env.Get("CMF_MCP_OAUTH_PASSWORD")) != 24 || !p.Record.SetIssuer || !p.Record.SetPassword {
		t.Fatalf("%+v", p.Record)
	}
	undo := UndoPlan(&p.Record, p.Env)
	if len(undo.Commands) != 1 || undo.Commands[0].String() != "/usr/local/bin/tailscale funnel --https=10000 off" ||
		undo.Env.Has("CMF_MCP_ISSUER_URL") || undo.Env.Has("CMF_MCP_OAUTH_PASSWORD") {
		t.Fatalf("%+v %s", undo.Commands, undo.Env.Bytes())
	}
}

func TestEveryFunnelPortTaken(t *testing.T) {
	ts := detect(t, `{"Web":{"h:443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:1"}}},"h:8443":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:2"}}},"h:10000":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:3"}}}}}`)
	if _, err := PlanTailscale(ts, &home.Env{}, 8000); err == nil || !strings.Contains(err.Error(), "every Funnel port") {
		t.Fatal(err)
	}
}

func TestTailscaleNotReady(t *testing.T) {
	if _, err := PlanTailscale(Tool{Kind: Tailscale}, &home.Env{}, 8000); err == nil || !strings.Contains(err.Error(), "isn't installed") {
		t.Fatal(err)
	}
	if _, err := PlanTailscale(Tool{Kind: Tailscale, Installed: true, Running: true, Host: "h"}, &home.Env{}, 8000); err == nil || !strings.Contains(err.Error(), "doesn't allow Funnel") {
		t.Fatal(err)
	}
}

func TestUseURL(t *testing.T) {
	for _, bad := range []string{"http://x.example", "https://x.example/mcp", "x.example"} {
		if _, err := PlanURL(bad, &home.Env{}); err == nil {
			t.Errorf("%s should be refused", bad)
		}
	}
	p, err := PlanURL("https://cmf.example.com/", &home.Env{})
	if err != nil || p.Record.PublicURL != "https://cmf.example.com" || len(p.Commands) != 0 {
		t.Fatalf("%v %+v", err, p)
	}
	q, _ := PlanURL("https://abc-def.trycloudflare.com", &home.Env{})
	if !strings.Contains(strings.Join(q.Lines, " "), "new address every run") {
		t.Fatal(q.Lines)
	}
	// Undo puts a previous issuer back and keeps the password it didn't make.
	env := &home.Env{}
	env.Set("CMF_MCP_ISSUER_URL", "https://old.example.com")
	env.Set("CMF_MCP_OAUTH_PASSWORD", "pw")
	r, _ := PlanURL("https://new.example.com", env)
	u := UndoPlan(&r.Record, r.Env)
	if u.Env.Get("CMF_MCP_ISSUER_URL") != "https://old.example.com" || u.Env.Get("CMF_MCP_OAUTH_PASSWORD") != "pw" {
		t.Fatal(string(u.Env.Bytes()))
	}
}

// The FalkorDB Browser button: 8443 serves port 3001 on Todd's tailnet.
func TestURLForAServedPort(t *testing.T) {
	ts := detect(t, toddFunnel)
	if got := ts.URLFor(3001); got != "https://mac.tail1.ts.net:8443" {
		t.Fatalf("got %q", got)
	}
	if got := ts.URLFor(8000); got != "https://mac.tail1.ts.net" {
		t.Fatalf("got %q", got)
	}
	if got := ts.URLFor(6379); got != "" {
		t.Fatalf("got %q", got)
	}
}
