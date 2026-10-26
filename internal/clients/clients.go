// Package clients reads and writes the CMF entry in each AI app's own MCP
// config file (docs/setup-simplification-plan.md, item 3.4): exactly one
// entry per app, duplicates removed, a backup first, and the file read back
// after writing. Hooks and agent instructions are capture wiring (step O).
package clients

import (
	"bytes"
	"errors"
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/tmargolis/context-memory-fabric/internal/jsonedit"
)

// Name is the entry name CMF writes (Codex's TOML table name can't have dashes).
const (
	Name      = "context-memory-fabric"
	CodexName = "context_memory_fabric"
)

type Format int

const (
	JSONMCPServers Format = iota // {"mcpServers": {name: entry}}
	JSONServers                  // VS Code: {"servers": {name: entry}}
	TOMLCodex                    // [mcp_servers.<name>]
)

// Style is the shape of the entry an app gets.
type Style int

const (
	Bridge   Style = iota // command npx mcp-remote <url>: apps that only start commands (needs Node)
	HTTP                  // {"type": "http", "url": ...}
	URLOnly               // {"url": ...} (Cursor)
	CodexURL              // url = "..." in the Codex table
)

type App struct {
	ID, Label string
	Format    Format
	Style     Style
	// AfterNote is what the user does for the app to pick the change up.
	AfterNote string
	path      func(Env) string
	marker    func(Env) string // its existence means the app is installed
}

// Env locates the per-user config folders.
type Env struct {
	Home    string
	OS      string // runtime.GOOS
	AppData string // %APPDATA% on Windows
	Getenv  func(string) string
}

func (e Env) getenv(k string) string {
	if e.Getenv == nil {
		return ""
	}
	return e.Getenv(k)
}

// configDir is the OS's per-user application config folder.
func (e Env) configDir() string {
	switch e.OS {
	case "darwin":
		return filepath.Join(e.Home, "Library", "Application Support")
	case "windows":
		if e.AppData != "" {
			return e.AppData
		}
		return filepath.Join(e.Home, "AppData", "Roaming")
	}
	if x := e.getenv("XDG_CONFIG_HOME"); x != "" {
		return x
	}
	return filepath.Join(e.Home, ".config")
}

func (e Env) codexHome() string {
	if c := e.getenv("CODEX_HOME"); c != "" {
		return c
	}
	return filepath.Join(e.Home, ".codex")
}

var Apps = []App{
	{ID: "claude-desktop", Label: "Claude Desktop", Format: JSONMCPServers, Style: Bridge,
		AfterNote: "Quit Claude Desktop completely and reopen it, then start a new session.",
		path:      func(e Env) string { return filepath.Join(e.configDir(), "Claude", "claude_desktop_config.json") },
		marker:    func(e Env) string { return filepath.Join(e.configDir(), "Claude") }},
	{ID: "claude-code", Label: "Claude Code", Format: JSONMCPServers, Style: HTTP,
		AfterNote: "Start a new Claude Code session.",
		path:      func(e Env) string { return filepath.Join(e.Home, ".claude.json") },
		marker:    func(e Env) string { return filepath.Join(e.Home, ".claude") }},
	{ID: "codex", Label: "Codex", Format: TOMLCodex, Style: CodexURL,
		AfterNote: "Restart Codex.",
		path:      func(e Env) string { return filepath.Join(e.codexHome(), "config.toml") },
		marker:    func(e Env) string { return e.codexHome() }},
	{ID: "cursor", Label: "Cursor", Format: JSONMCPServers, Style: URLOnly,
		AfterNote: "Reload Cursor.",
		path:      func(e Env) string { return filepath.Join(e.Home, ".cursor", "mcp.json") },
		marker:    func(e Env) string { return filepath.Join(e.Home, ".cursor") }},
	{ID: "vscode", Label: "VS Code", Format: JSONServers, Style: HTTP,
		AfterNote: "Reload VS Code.",
		path:      func(e Env) string { return filepath.Join(e.configDir(), "Code", "User", "mcp.json") },
		marker:    func(e Env) string { return filepath.Join(e.configDir(), "Code", "User") }},
	{ID: "antigravity", Label: "Antigravity", Format: JSONMCPServers, Style: Bridge,
		AfterNote: "Restart Antigravity.",
		path:      antigravityConfig,
		marker:    func(e Env) string { return filepath.Join(e.Home, ".gemini") }},
}

func Find(id string) (App, bool) {
	for _, a := range Apps {
		if a.ID == id {
			return a, true
		}
	}
	return App{}, false
}

func IDs() []string {
	ids := make([]string, len(Apps))
	for i, a := range Apps {
		ids[i] = a.ID
	}
	return ids
}

func (a App) Path(e Env) string { return a.path(e) }

// Target is the CMF server the entries should reach.
type Target struct {
	Port      int
	PublicURL string // CMF_MCP_ISSUER_URL, when OAuth over a public URL is set up
	Token     string // CMF_MCP_AUTH_TOKEN, sent as a bearer header; never shown
	// Bridge is this cmf binary's path, for entries that start `cmf bridge`
	// (item 3.8) instead of `npx mcp-remote`; "" when cmf runs from a place
	// that won't last (go run, a temp folder).
	Bridge string
	Home   string // CMF_HOME, when it isn't the default, for the bridge to find .env
}

func (t Target) LocalURL() string { return fmt.Sprintf("http://127.0.0.1:%d/mcp", t.Port) }

// NeedsToken: the server lets a local entry in only with the token, since
// either the static token or OAuth (remote access) guards it.
func (t Target) NeedsToken() bool { return t.Token != "" || t.PublicURL != "" }

// isLocal reports whether u is this computer's CMF address.
func (t Target) isLocal(u string) bool {
	p, err := url.Parse(strings.TrimSpace(u))
	if err != nil || p.Host == "" || strings.TrimSuffix(p.Path, "/") != "/mcp" {
		return false
	}
	host := p.Hostname()
	return (host == "127.0.0.1" || host == "localhost" || host == "::1") && p.Port() == fmt.Sprint(t.Port)
}

// isPublic reports whether u is this CMF's public address (remote access).
func (t Target) isPublic(u string) bool {
	p, err := url.Parse(strings.TrimSpace(u))
	if err != nil || p.Host == "" || strings.TrimSuffix(p.Path, "/") != "/mcp" || t.PublicURL == "" {
		return false
	}
	pub, err := url.Parse(t.PublicURL)
	return err == nil && pub.Host != "" && strings.EqualFold(pub.Host, p.Host) && p.Scheme == pub.Scheme
}

// accepts reports whether u reaches this CMF: its local port or its public URL.
func (t Target) accepts(u string) bool { return t.isLocal(u) || t.isPublic(u) }

// works reports whether an entry at u carrying token gets in. Apps on this
// computer use the local address with the token whenever one exists; the
// public address (its own OAuth sign-in, which can lapse) only until then.
func (t Target) works(u, token string) bool {
	switch {
	case t.isLocal(u):
		return !t.NeedsToken() || (t.Token != "" && token == t.Token)
	case t.isPublic(u):
		return t.Token == ""
	}
	return false
}

// Entry is one MCP server entry in an app's config.
type Entry struct {
	Name    string `json:"name"`
	URL     string `json:"url,omitempty"` // where it points, when it points at a URL
	CMF     bool   `json:"cmf"`           // a CMF entry (by name or target)
	Correct bool   `json:"correct"`       // a CMF entry that reaches this install and gets in
	Kind    string `json:"kind"`          // "url", "bridge", "stdio" or "other"
	// Problem says why a CMF entry isn't correct, in plain words.
	Problem string `json:"problem,omitempty"`
}

type State struct {
	App       App     `json:"-"`
	ID        string  `json:"id"`
	Label     string  `json:"label"`
	Path      string  `json:"path"`
	Installed bool    `json:"installed"`
	Exists    bool    `json:"exists"`
	Entries   []Entry `json:"entries,omitempty"`
	Err       string  `json:"error,omitempty"`
}

// CMF returns the state's CMF entries.
func (s State) CMF() []Entry {
	var out []Entry
	for _, e := range s.Entries {
		if e.CMF {
			out = append(out, e)
		}
	}
	return out
}

// Healthy: exactly one CMF entry, and it reaches this install.
func (s State) Healthy() bool {
	c := s.CMF()
	return s.Err == "" && len(c) == 1 && c[0].Correct
}

var cmfName = regexp.MustCompile(`(?i)context[-_ ]?memory|mem(ory)?[-_ ]?fabric|^cmf([-_].*)?$`)

// classify describes one parsed entry. fields are its top-level string
// values (command, url, serverUrl), args its argument list, token the bearer
// token its Authorization header carries ("" for none).
// antigravityConfig: Antigravity keeps its settings in ~/.gemini/config/
// (mcp_config.json, hooks.json, AGENTS.md) since its 2026 standalone app;
// earlier versions used ~/.gemini/antigravity/mcp_config.json, which CMF
// kept writing after the move (found 2026-10-26: the live entry, on the
// public address with a stale OAuth client, was never seen).
func antigravityConfig(e Env) string {
	if _, err := os.Stat(filepath.Join(e.Home, ".gemini", "config")); err == nil {
		return filepath.Join(e.Home, ".gemini", "config", "mcp_config.json")
	}
	return filepath.Join(e.Home, ".gemini", "antigravity", "mcp_config.json")
}

// isCmfProgram: the cmf binary under its own name or a release download's
// (cmf-macos-universal, cmf-linux-amd64, cmf-windows.exe). Matching only
// "cmf" rejected the bridge entry cmf had just written when it ran as
// cmf-macos-universal (found 2026-10-26, Antigravity).
func isCmfProgram(command string) bool {
	base := strings.TrimSuffix(filepath.Base(command), ".exe")
	return base == "cmf" || strings.HasPrefix(base, "cmf-")
}

func classify(name string, fields map[string]string, args []string, token string, t Target) Entry {
	e := Entry{Name: name, Kind: "other"}
	for _, k := range []string{"url", "serverUrl", "httpUrl"} {
		if u := fields[k]; u != "" {
			e.URL, e.Kind = u, "url"
		}
	}
	joined := strings.Join(args, " ")
	if isCmfProgram(fields["command"]) && len(args) > 0 && args[0] == "bridge" {
		// cmf bridge reads the port from ~/.cmf/.env, so it reaches this
		// install for as long as the binary is where the entry says.
		e.Kind, e.URL = "cmf-bridge", t.LocalURL()
		for i, a := range args {
			if a == "--url" && i+1 < len(args) {
				e.URL = args[i+1]
			} else if strings.HasPrefix(a, "--url=") {
				e.URL = strings.TrimPrefix(a, "--url=")
			}
		}
		_, err := os.Stat(fields["command"])
		e.Correct = err == nil && t.accepts(e.URL)
		e.CMF = true
		if !e.Correct {
			e.Problem = "runs a cmf bridge that isn't there any more"
			if err == nil {
				e.Problem = "points at " + e.URL + ", not this install"
			}
		}
		return e
	}
	if e.URL == "" && strings.Contains(joined, "mcp-remote") {
		e.Kind = "bridge"
		for _, a := range args {
			if strings.HasPrefix(a, "http://") || strings.HasPrefix(a, "https://") {
				e.URL = a
			}
		}
	}
	if e.Kind == "other" && (strings.Contains(joined, "server.mcp") || strings.Contains(fields["command"], "server.mcp")) {
		e.Kind = "stdio" // a CMF server started by the app itself (developer setups)
	}
	reaches := e.URL != "" && t.accepts(e.URL)
	e.Correct = reaches && t.works(e.URL, token)
	e.CMF = cmfName.MatchString(name) || reaches || e.Kind == "stdio"
	switch {
	case e.Correct || !e.CMF:
	case e.Kind == "stdio":
		e.Problem = "starts its own CMF server"
	case !reaches:
		e.Problem = "points at " + e.URL + ", not this install"
	case t.isPublic(e.URL):
		e.Problem = "signs in over the public address (OAuth), which can lapse; apps on this computer use the local address and token"
	case token == "":
		e.Problem = "has no access token, so the server refuses it"
	default:
		e.Problem = "has an old access token, so the server refuses it"
	}
	return e
}

// Inspect reads one app's config file. A missing file is not an error.
func Inspect(a App, env Env, t Target) State {
	s := State{App: a, ID: a.ID, Label: a.Label, Path: a.Path(env)}
	_, err := os.Stat(a.marker(env))
	s.Installed = err == nil
	data, err := os.ReadFile(s.Path)
	switch {
	case errors.Is(err, os.ErrNotExist):
		return s
	case err != nil:
		s.Err = err.Error()
		return s
	}
	s.Exists, s.Installed = true, true
	s.Entries, err = parseEntries(a.Format, data, t)
	if err != nil {
		s.Err = fmt.Sprintf("can't read %s: %v", s.Path, err)
	}
	return s
}

func InspectAll(env Env, t Target) []State {
	out := make([]State, 0, len(Apps))
	for _, a := range Apps {
		out = append(out, Inspect(a, env, t))
	}
	return out
}

func parseEntries(f Format, data []byte, t Target) ([]Entry, error) {
	if f == TOMLCodex {
		return codexEntries(string(data), t), nil
	}
	if len(bytes.TrimSpace(data)) == 0 {
		return nil, nil
	}
	root, err := jsonedit.Parse(data)
	if err != nil {
		return nil, err
	}
	servers, err := root.Child(serversKey(f))
	if err != nil {
		return nil, fmt.Errorf("%s: %w", serversKey(f), err)
	}
	var out []Entry
	for _, name := range servers.Keys() {
		raw, _ := servers.Get(name)
		fields, args, token := entryFields(raw)
		out = append(out, classify(name, fields, args, token, t))
	}
	return out, nil
}

func serversKey(f Format) string {
	if f == JSONServers {
		return "servers"
	}
	return "mcpServers"
}
