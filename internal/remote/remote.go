// Package remote is the Remote access helper (docs/setup-simplification-plan.md,
// item 3.10): connectors in ChatGPT, Gemini or claude.ai reach CMF only
// through a public HTTPS URL, and only with OAuth on. It finds the tunnel
// tools the user already has (Tailscale, cloudflared, ngrok), turns OAuth on
// first, runs only the command that exposes CMF's own port, and records what
// it added so undo removes exactly that. It never installs, signs in to or
// reconfigures a tunnel beyond CMF's port.
package remote

import (
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"math/big"
	"net/url"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/home"
	"github.com/tmargolis/context-memory-fabric/internal/run"
)

const (
	Tailscale   = "tailscale"
	Cloudflared = "cloudflared"
	Ngrok       = "ngrok"
	RecordFile  = "remote.json" // in ~/.cmf/state: what `enable` added
)

// FunnelPorts are the HTTPS ports Tailscale Funnel can publish.
var FunnelPorts = []int{443, 8443, 10000}

// Tool is one tunnel program on this machine.
type Tool struct {
	Kind      string `json:"kind"`
	Path      string `json:"path,omitempty"`
	Installed bool   `json:"installed"`
	Running   bool   `json:"running"`          // Tailscale: connected to its tailnet
	Host      string `json:"host,omitempty"`   // Tailscale: this machine's DNS name
	Funnel    bool   `json:"funnel,omitempty"` // Tailscale: the tailnet lets this node use Funnel
	// ExposedURL is the public URL already forwarding to CMF's port, if any.
	ExposedURL string `json:"exposed_url,omitempty"`
	// Taken: HTTPS ports already serving something, and what.
	Taken  map[int]string `json:"taken,omitempty"`
	Detail string         `json:"detail,omitempty"`
}

// Finder locates a program; replaced in tests.
type Finder func(name string) (string, bool)

var tailscaleApp = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"

// Detect looks at each tunnel tool. Read-only.
func Detect(ctx context.Context, r run.Runner, find Finder, port int) []Tool {
	var out []Tool
	ts := Tool{Kind: Tailscale}
	if p, ok := find("tailscale"); ok {
		ts.Path, ts.Installed = p, true
	} else if p, ok := find(tailscaleApp); ok {
		ts.Path, ts.Installed = p, true
	}
	if ts.Installed {
		inspectTailscale(ctx, r, &ts, port)
	} else {
		ts.Detail = "not installed (https://tailscale.com/download)"
	}
	out = append(out, ts)
	for _, kind := range []string{Cloudflared, Ngrok} {
		t := Tool{Kind: kind}
		if p, ok := find(kind); ok {
			t.Path, t.Installed = p, true
			t.Detail = "installed; set up a tunnel with a lasting hostname yourself, then `cmf remote use https://<host>`"
		} else {
			t.Detail = "not installed"
		}
		out = append(out, t)
	}
	return out
}

func inspectTailscale(ctx context.Context, r run.Runner, t *Tool, port int) {
	res := r.Run(ctx, run.Cmd{Name: t.Path, Args: []string{"status", "--json"}, Timeout: 10 * time.Second})
	var st struct {
		BackendState string
		Self         struct {
			DNSName      string
			Capabilities []string
			CapMap       map[string]any
		}
	}
	if res.Err != nil || json.Unmarshal([]byte(res.Stdout), &st) != nil {
		t.Detail = "installed, but `tailscale status` didn't answer"
		return
	}
	t.Running = st.BackendState == "Running"
	t.Host = strings.TrimSuffix(st.Self.DNSName, ".")
	for _, c := range st.Self.Capabilities {
		t.Funnel = t.Funnel || strings.Contains(c, "funnel")
	}
	for c := range st.Self.CapMap {
		t.Funnel = t.Funnel || strings.Contains(c, "funnel")
	}
	if !t.Running {
		t.Detail = "installed, not connected (" + st.BackendState + "); sign in to Tailscale first"
		return
	}
	res = r.Run(ctx, run.Cmd{Name: t.Path, Args: []string{"funnel", "status", "--json"}, Timeout: 10 * time.Second})
	var serve struct {
		Web map[string]struct {
			Handlers map[string]struct{ Proxy string }
		}
		AllowFunnel map[string]bool
	}
	json.Unmarshal([]byte(res.Stdout), &serve)
	t.Taken = map[int]string{}
	for hostPort, web := range serve.Web {
		_, p, _ := strings.Cut(hostPort, ":")
		https, _ := strconv.Atoi(p)
		for path, h := range web.Handlers {
			t.Taken[https] = h.Proxy
			if path == "/" && proxiesTo(h.Proxy, port) && serve.AllowFunnel[hostPort] {
				t.ExposedURL = publicURL(t.Host, https)
			}
		}
	}
	switch {
	case t.ExposedURL != "":
		t.Detail = "Funnel publishes CMF at " + t.ExposedURL
	case !t.Funnel:
		t.Detail = "connected, but this tailnet doesn't allow Funnel for this machine (turn it on in the Tailscale admin console's access controls)"
	default:
		t.Detail = "connected; Funnel can publish CMF at https://" + t.Host
	}
}

// URLFor is the https address Tailscale Serve (tailnet) or Funnel (public)
// gives a local port, lowest HTTPS port first; "" if it serves none.
func (t Tool) URLFor(port int) string {
	var ports []int
	for https, proxy := range t.Taken {
		if proxiesTo(proxy, port) {
			ports = append(ports, https)
		}
	}
	if len(ports) == 0 || t.Host == "" {
		return ""
	}
	slices.Sort(ports)
	return publicURL(t.Host, ports[0])
}

// ServedURL is URLFor on this machine's Tailscale, "" without one.
func ServedURL(ctx context.Context, r run.Runner, find Finder, port int) string {
	for _, t := range Detect(ctx, r, find, port) {
		if t.Kind == Tailscale && t.Running {
			return t.URLFor(port)
		}
	}
	return ""
}

func proxiesTo(proxy string, port int) bool {
	u, err := url.Parse(proxy)
	if err != nil {
		return false
	}
	h := u.Hostname()
	return (h == "127.0.0.1" || h == "localhost") && u.Port() == strconv.Itoa(port)
}

func publicURL(host string, https int) string {
	if https == 443 {
		return "https://" + host
	}
	return fmt.Sprintf("https://%s:%d", host, https)
}

// Record is what `enable` added, so `disable` removes only that.
type Record struct {
	Kind          string `json:"kind"`
	PublicURL     string `json:"public_url"`
	HTTPSPort     int    `json:"https_port,omitempty"` // Tailscale: the Funnel port it opened
	SetIssuer     bool   `json:"set_issuer"`           // it set CMF_MCP_ISSUER_URL
	SetPassword   bool   `json:"set_password"`         // it generated CMF_MCP_OAUTH_PASSWORD
	PriorIssuer   string `json:"prior_issuer,omitempty"`
	TailscalePath string `json:"tailscale_path,omitempty"`
}

func recordPath(h home.Home) string { return filepath.Join(h.Dir, "state", RecordFile) }

func LoadRecord(h home.Home) (*Record, error) {
	data, err := os.ReadFile(recordPath(h))
	if err != nil {
		return nil, err
	}
	var rec Record
	return &rec, json.Unmarshal(data, &rec)
}

func SaveRecord(h home.Home, rec *Record) error {
	data, _ := json.MarshalIndent(rec, "", "  ")
	return home.WriteFile(recordPath(h), append(data, '\n'), 0o600)
}

func RemoveRecord(h home.Home) error {
	err := os.Remove(recordPath(h))
	if os.IsNotExist(err) {
		return nil
	}
	return err
}

// Plan is what enabling remote access will do.
type Plan struct {
	Record   Record
	Env      *home.Env // .env with OAuth on
	Commands []run.Cmd // the one CMF-specific expose command, if any
	Lines    []string  // for review
}

// Password makes an OAuth password for the consent page.
func Password() string {
	const alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
	b := make([]byte, 24)
	for i := range b {
		n, _ := rand.Int(rand.Reader, big.NewInt(int64(len(alphabet))))
		b[i] = alphabet[n.Int64()]
	}
	return string(b)
}

// oauth turns OAuth on for publicURL in env, noting what it set.
func oauth(env *home.Env, publicURL string, rec *Record, lines *[]string) {
	rec.PriorIssuer = env.Get("CMF_MCP_ISSUER_URL")
	if rec.PriorIssuer != publicURL {
		env.Set("CMF_MCP_ISSUER_URL", publicURL)
		rec.SetIssuer = true
		*lines = append(*lines, "Turn on OAuth: set CMF_MCP_ISSUER_URL="+publicURL+" in ~/.cmf/.env (backup kept)")
	}
	if strings.TrimSpace(env.Get("CMF_MCP_OAUTH_PASSWORD")) == "" {
		env.Set("CMF_MCP_OAUTH_PASSWORD", Password())
		rec.SetPassword = true
		*lines = append(*lines, "Generate CMF_MCP_OAUTH_PASSWORD (the consent page asks for it when a connector is added)")
	}
	if env.Get("CMF_MCP_AUTH_TOKEN") != "" {
		*lines = append(*lines, "Note: with OAuth on, the server ignores CMF_MCP_AUTH_TOKEN; local apps keep working through `cmf bridge` only if they can do OAuth")
	}
}

// PlanTailscale exposes CMF's port through Funnel on the first free HTTPS port.
func PlanTailscale(t Tool, env *home.Env, port int) (*Plan, error) {
	switch {
	case !t.Installed:
		return nil, fmt.Errorf("Tailscale isn't installed; install it from https://tailscale.com/download and sign in, or use your own tunnel with `cmf remote use <url>`")
	case !t.Running:
		return nil, fmt.Errorf("Tailscale isn't connected; open it and sign in first")
	case !t.Funnel && t.ExposedURL == "":
		return nil, fmt.Errorf("this tailnet doesn't allow Funnel for this machine; allow it in the Tailscale admin console (Access controls → funnel), then try again")
	}
	p := &Plan{Env: env.Clone(), Record: Record{Kind: Tailscale, TailscalePath: t.Path}}
	if t.ExposedURL != "" {
		p.Record.PublicURL = t.ExposedURL
		p.Lines = append(p.Lines, "Funnel already publishes CMF at "+t.ExposedURL+"; nothing to open")
	} else {
		https := 0
		for _, candidate := range FunnelPorts {
			if _, used := t.Taken[candidate]; !used {
				https = candidate
				break
			}
		}
		if https == 0 {
			return nil, fmt.Errorf("every Funnel port (443, 8443, 10000) already serves something else on this machine; free one, or use `cmf remote use <url>`")
		}
		p.Record.PublicURL, p.Record.HTTPSPort = publicURL(t.Host, https), https
		cmd := run.Cmd{Name: t.Path, Args: []string{"funnel", "--bg", "--https=" + strconv.Itoa(https), fmt.Sprintf("http://127.0.0.1:%d", port)}, Timeout: 30 * time.Second}
		p.Commands = append(p.Commands, cmd)
		p.Lines = append(p.Lines, fmt.Sprintf("Publish only CMF's port %d at %s: `%s` (other Tailscale settings untouched)", port, p.Record.PublicURL, cmd))
	}
	oauth(p.Env, p.Record.PublicURL, &p.Record, &p.Lines)
	return p, nil
}

// PlanURL is for a tunnel the user runs themselves: OAuth for their URL.
func PlanURL(raw string, env *home.Env) (*Plan, error) {
	u, err := url.Parse(strings.TrimRight(strings.TrimSpace(raw), "/"))
	if err != nil || u.Scheme != "https" || u.Host == "" || (u.Path != "" && u.Path != "/") {
		return nil, fmt.Errorf("%q: give the tunnel's public HTTPS address, like https://cmf.example.com (no /mcp)", raw)
	}
	p := &Plan{Env: env.Clone(), Record: Record{Kind: "url", PublicURL: u.Scheme + "://" + u.Host}}
	if strings.HasSuffix(u.Hostname(), ".trycloudflare.com") {
		p.Lines = append(p.Lines, "Warning: a Cloudflare quick tunnel gets a new address every run, and OAuth clients stop working when it changes; a named tunnel keeps one")
	}
	oauth(p.Env, p.Record.PublicURL, &p.Record, &p.Lines)
	return p, nil
}

// UndoPlan reverses a Record: closes the Funnel port it opened and takes out
// the OAuth settings it set (the password only if it generated it).
func UndoPlan(rec *Record, env *home.Env) *Plan {
	p := &Plan{Env: env.Clone(), Record: *rec}
	if rec.Kind == Tailscale && rec.HTTPSPort != 0 {
		cmd := run.Cmd{Name: rec.TailscalePath, Args: []string{"funnel", "--https=" + strconv.Itoa(rec.HTTPSPort), "off"}, Timeout: 30 * time.Second}
		p.Commands = append(p.Commands, cmd)
		p.Lines = append(p.Lines, "Stop publishing "+rec.PublicURL+": `"+cmd.String()+"`")
	}
	if rec.SetIssuer {
		if rec.PriorIssuer != "" {
			p.Env.Set("CMF_MCP_ISSUER_URL", rec.PriorIssuer)
			p.Lines = append(p.Lines, "Set CMF_MCP_ISSUER_URL back to "+rec.PriorIssuer)
		} else {
			p.Env.Unset("CMF_MCP_ISSUER_URL")
			p.Lines = append(p.Lines, "Turn OAuth off: remove CMF_MCP_ISSUER_URL")
		}
	}
	if rec.SetPassword && !(rec.SetIssuer && rec.PriorIssuer != "") {
		p.Env.Unset("CMF_MCP_OAUTH_PASSWORD")
		p.Lines = append(p.Lines, "Remove the generated CMF_MCP_OAUTH_PASSWORD")
	}
	return p
}
