// Package capture is CMF management's capture wiring (docs/setup-simplification-
// plan.md, item 3.11 and section 3.9): the Stop-hook nudge that asks an AI
// app to call capture_session at checkpoints, its installers, the
// per-source report (hook / poller / server instructions), volume-based
// estimates and the marker-fenced agent block.
package capture

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

// The nudge is a Go port of server/adapters/capture_nudge.py, so an app's
// Stop hook can run `cmf hook stop` with no Python on the machine. Same
// throttle, loop guard, outputs and marker; a test keeps Reason identical.
const (
	Marker            = "cmf-capture-nudge"
	DefaultMinTurns   = 5
	DefaultMinMinutes = 30.0
	stateTTL          = 7 * 24 * time.Hour
)

const Reason = "CMF checkpoint. Before finishing: if the work since the last checkpoint " +
	"reached a decision, a conclusion, a milestone, or durable knowledge that " +
	"would still be useful read cold later, call the Context Memory Fabric " +
	"`capture_session` tool with one item per distinct fact (destination=" +
	"'episode' for what happened or was decided, 'doc_proposal' for durable " +
	"reusable knowledge). Skip anything already captured, routine steps, and " +
	"secrets or credentials. If nothing qualifies, or the tool isn't available " +
	"here, don't call it. Then finish exactly as you were going to; don't " +
	"mention this checkpoint unless you captured something."

var Harnesses = []string{"claude_code", "codex", "antigravity"}

type entry struct {
	Stops       int    `json:"stops"`
	WindowStart string `json:"window_start"`
	Pending     bool   `json:"pending"`
	LastSeen    string `json:"last_seen"`
	Nudges      int    `json:"nudges,omitempty"`
	LastNudge   string `json:"last_nudge,omitempty"`
}

type nudgeState struct {
	Sessions map[string]*entry `json:"sessions"`
}

func sessionKey(harness string, payload map[string]any) string {
	field := "session_id"
	if harness == "antigravity" {
		field = "conversationId"
	}
	sid, _ := payload[field].(string)
	if sid == "" {
		return ""
	}
	return harness + ":" + sid
}

func shouldSkip(harness string, payload map[string]any) bool {
	if harness == "claude_code" || harness == "codex" {
		if active, _ := payload["stop_hook_active"].(bool); active {
			return true
		}
	}
	if harness == "antigravity" {
		// Only a real, finished, error-free stop.
		if e, ok := payload["error"]; ok && e != nil && e != false && e != "" {
			return true
		}
		if idle, ok := payload["fullyIdle"].(bool); ok && !idle {
			return true
		}
	}
	return false
}

func decide(harness string, payload map[string]any, st *nudgeState, now time.Time, minTurns int, minMinutes float64) bool {
	key := sessionKey(harness, payload)
	if key == "" {
		return false
	}
	if st.Sessions == nil {
		st.Sessions = map[string]*entry{}
	}
	e := st.Sessions[key]
	if e == nil {
		e = &entry{WindowStart: now.Format(time.RFC3339Nano)}
		st.Sessions[key] = e
	}
	e.LastSeen = now.Format(time.RFC3339Nano)
	if e.Pending {
		// The stop that ends the agent's reply to our own nudge.
		e.Pending = false
		return false
	}
	if shouldSkip(harness, payload) {
		return false
	}
	e.Stops++
	start, err := time.Parse(time.RFC3339Nano, e.WindowStart)
	if err != nil {
		start, e.WindowStart = now, now.Format(time.RFC3339Nano)
	}
	minutes := now.Sub(start).Minutes()
	if e.Stops >= minTurns || (e.Stops >= 2 && minutes >= minMinutes) {
		e.Stops, e.WindowStart, e.Pending = 0, now.Format(time.RFC3339Nano), true
		e.Nudges++
		e.LastNudge = now.Format(time.RFC3339Nano)
		return true
	}
	return false
}

// Render is what the hook prints.
func Render(harness string, nudge bool) map[string]any {
	return render(harness, nudge, "")
}

func render(harness string, nudge bool, project string) map[string]any {
	reason := Reason
	if project != "" {
		reason += fmt.Sprintf(" If you call it, pass project=%q.", project)
	}
	if harness == "antigravity" {
		if nudge {
			return map[string]any{"decision": "continue", "reason": reason}
		}
		return map[string]any{"decision": "stop"}
	}
	if nudge {
		return map[string]any{"decision": "block", "reason": reason}
	}
	return map[string]any{}
}

var nonSlug = regexp.MustCompile(`[^a-z0-9]+`)

// ProjectHint is the session's project for capture_session: the name of the
// git repository holding cwd, as a slug, or "" outside one. The server only
// sees an MCP call, so without it live captures were filed under `misc`
// (B19, 2026-10-26).
func ProjectHint(cwd string) string {
	if cwd == "" {
		return ""
	}
	dir, err := filepath.Abs(cwd)
	if err != nil {
		return ""
	}
	for {
		if _, err := os.Stat(filepath.Join(dir, ".git")); err == nil {
			return strings.Trim(nonSlug.ReplaceAllString(strings.ToLower(filepath.Base(dir)), "-"), "-")
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return ""
		}
		dir = parent
	}
}

func envNum(name string, def float64) float64 {
	if v, err := strconv.ParseFloat(os.Getenv(name), 64); err == nil {
		return v
	}
	return def
}

// RunHook is the hook body: the Stop payload in, the decision out. It fails
// open: any error lets the session stop.
func RunHook(harness string, stdin []byte, statePath string, now time.Time) (out map[string]any) {
	defer func() {
		if recover() != nil {
			out = Render(harness, false)
		}
	}()
	var payload map[string]any
	if len(stdin) > 0 && json.Unmarshal(stdin, &payload) != nil {
		payload = nil
	}
	if payload == nil {
		payload = map[string]any{}
	}
	var st nudgeState
	if data, err := os.ReadFile(statePath); err == nil {
		json.Unmarshal(data, &st)
	}
	nudge := decide(harness, payload, &st, now, int(envNum("CMF_NUDGE_MIN_TURNS", DefaultMinTurns)), envNum("CMF_NUDGE_MIN_MINUTES", DefaultMinMinutes))
	for k, e := range st.Sessions {
		if seen, err := time.Parse(time.RFC3339Nano, e.LastSeen); err != nil || now.Sub(seen) > stateTTL {
			delete(st.Sessions, k)
		}
	}
	if err := os.MkdirAll(filepath.Dir(statePath), 0o700); err == nil {
		data, _ := json.MarshalIndent(st, "", " ")
		tmp := fmt.Sprintf("%s.tmp-%d", statePath, os.Getpid())
		if os.WriteFile(tmp, data, 0o600) == nil {
			os.Rename(tmp, statePath)
		}
	}
	cwd, _ := payload["cwd"].(string)
	return render(harness, nudge, ProjectHint(cwd))
}
