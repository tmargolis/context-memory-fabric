package capture

import (
	"encoding/json"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/home"
)

// pythonReason reads NUDGE_REASON out of a Python source file.
func pythonReason(t *testing.T, path string) string {
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	block := regexp.MustCompile(`(?s)NUDGE_REASON = \((.*?)\n\)`).FindSubmatch(data)
	if block == nil {
		t.Fatalf("no NUDGE_REASON in %s", path)
	}
	var b strings.Builder
	for _, m := range regexp.MustCompile(`"((?:[^"\\]|\\.)*)"`).FindAllSubmatch(block[1], -1) {
		b.Write(m[1])
	}
	return b.String()
}

func TestReasonMatchesPython(t *testing.T) {
	for _, p := range []string{"../../server/adapters/capture_nudge.py", "../../plugins/cmf-capture/hooks/cmf_nudge.py"} {
		if got := pythonReason(t, p); got != Reason {
			t.Errorf("%s differs:\n%s\n%s", p, got, Reason)
		}
	}
}

func run(t *testing.T, harness, payload, state string, at time.Time) map[string]any {
	return RunHook(harness, []byte(payload), state, at)
}

func TestThrottleAndLoopGuard(t *testing.T) {
	state := filepath.Join(t.TempDir(), "s.json")
	at := time.Date(2026, 10, 24, 12, 0, 0, 0, time.UTC)
	p := `{"session_id":"a"}`
	for i := 1; i <= 4; i++ {
		if out := run(t, "claude_code", p, state, at.Add(time.Duration(i)*time.Minute)); len(out) != 0 {
			t.Fatalf("stop %d nudged: %v", i, out)
		}
	}
	if out := run(t, "claude_code", p, state, at.Add(5*time.Minute)); out["decision"] != "block" || out["reason"] != Reason {
		t.Fatalf("5th stop: %v", out)
	}
	if out := run(t, "claude_code", p, state, at.Add(6*time.Minute)); len(out) != 0 {
		t.Fatal("the stop answering a nudge must pass")
	}
	if out := run(t, "claude_code", `{"session_id":"a","stop_hook_active":true}`, state, at.Add(7*time.Minute)); len(out) != 0 {
		t.Fatal("stop_hook_active must pass")
	}
	// Time-based: 2 stops and 30 minutes.
	q := `{"session_id":"b"}`
	run(t, "codex", q, state, at)
	if out := run(t, "codex", q, state, at.Add(31*time.Minute)); out["decision"] != "block" {
		t.Fatalf("time window: %v", out)
	}
}

func TestAntigravityShapeAndFailOpen(t *testing.T) {
	state := filepath.Join(t.TempDir(), "s.json")
	at := time.Date(2026, 10, 24, 12, 0, 0, 0, time.UTC)
	if out := run(t, "antigravity", `{"conversationId":"c","fullyIdle":false}`, state, at); out["decision"] != "stop" {
		t.Fatal(out)
	}
	if out := run(t, "antigravity", `not json`, state, at); out["decision"] != "stop" {
		t.Fatal("bad input must let the session stop", out)
	}
	os.WriteFile(state, []byte("{broken"), 0o600)
	if out := run(t, "claude_code", `{"session_id":"z"}`, state, at); len(out) != 0 {
		t.Fatal(out)
	}
}

func setupHome(t *testing.T) (string, *home.Backup) {
	dir := t.TempDir()
	b, _ := home.Home{Dir: filepath.Join(dir, ".cmf")}.NewBackup(time.Date(2026, 10, 24, 12, 0, 0, 0, time.UTC))
	return dir, b
}

func write(t *testing.T, path, body string) {
	os.MkdirAll(filepath.Dir(path), 0o700)
	os.WriteFile(path, []byte(body), 0o644)
}

func TestInstallKeepsOtherHooksAndNeverDuplicates(t *testing.T) {
	user, b := setupHome(t)
	settings := HookFile("claude_code", user)
	write(t, settings, `{"model":"opus","hooks":{"Stop":[{"matcher":"","hooks":[{"type":"command","command":"say done"}]}],"PreToolUse":[]},"zzz":1}`)
	c, err := PlanHook("claude_code", user, "/Applications/cmf", false)
	if err != nil || c.Empty() {
		t.Fatal(err)
	}
	if err := c.Write(b); err != nil {
		t.Fatal(err)
	}
	got, _ := os.ReadFile(settings)
	text := string(got)
	if !strings.Contains(text, "say done") || !strings.Contains(text, `\"/Applications/cmf\" hook stop --harness claude_code # cmf-capture-nudge`) ||
		strings.Index(text, `"model"`) > strings.Index(text, `"hooks"`) || strings.Index(text, `"hooks"`) > strings.Index(text, `"zzz"`) {
		t.Fatal(text)
	}
	if h := ReadHooks("claude_code", user); h.Nudge != "cmf" {
		t.Fatalf("%+v", h)
	}
	if c, _ := PlanHook("claude_code", user, "/Applications/cmf", false); !c.Empty() {
		t.Fatal("second install must change nothing")
	}
	c, _ = PlanHook("claude_code", user, "", true)
	c.Write(b)
	got, _ = os.ReadFile(settings)
	if strings.Contains(string(got), Marker) || !strings.Contains(string(got), "say done") {
		t.Fatal(string(got))
	}
}

func TestExistingNudgesCount(t *testing.T) {
	user, _ := setupHome(t)
	write(t, HookFile("claude_code", user), `{"enabledPlugins":{"cmf-capture@context-memory-fabric":true}}`)
	write(t, HookFile("codex", user), `{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"sh -c 'tail' # cmf-codex-worker"}]},{"hooks":[{"type":"command","command":"/repo/.venv/bin/python3 -m server.adapters.capture_nudge run --harness codex # cmf-capture-nudge"}]}]}}`)
	write(t, HookFile("antigravity", user), `{"cmf-antigravity-worker":{"Stop":[{"type":"command","command":"x # cmf-antigravity-worker"}]}}`)
	if h := ReadHooks("claude_code", user); h.Nudge != "plugin" {
		t.Fatal(h)
	}
	if h := ReadHooks("codex", user); h.Nudge != "python" || !h.Worker {
		t.Fatal(h)
	}
	if c, _ := PlanHook("claude_code", user, "/Applications/cmf", false); !c.Empty() || !strings.Contains(c.Note, "already") {
		t.Fatalf("%+v", c)
	}
	// The older Python hook is replaced (it used to stop at "already has"),
	// in place; the worker hook beside it stays.
	c, _ := PlanHook("codex", user, "/Applications/cmf", false)
	if c.Empty() || c.Note != "replace the Python checkpoint hook with one running cmf" ||
		strings.Contains(string(c.New), "capture_nudge run") || !strings.Contains(string(c.New), `hook stop --harness codex`) ||
		!strings.Contains(string(c.New), "cmf-codex-worker") || strings.Count(string(c.New), Marker) != 1 {
		t.Fatalf("%s", c.New)
	}
	c, err := PlanHook("antigravity", user, "/Applications/cmf", false)
	if err != nil || c.Empty() {
		t.Fatal(err)
	}
	var cfg map[string]any
	json.Unmarshal(c.New, &cfg)
	if cfg["cmf-antigravity-worker"] == nil || cfg[Marker] == nil {
		t.Fatal(string(c.New))
	}
	if _, err := PlanHook("claude_code", user, "", false); err != nil {
		t.Fatal("already captured: no path needed", err)
	}
	if _, err := PlanHook("codex", user, "", false); err == nil {
		t.Fatal("replacing the Python hook needs a lasting cmf path")
	}
	if _, err := PlanHook("antigravity", user, "", false); err == nil {
		t.Fatal("installing needs a lasting cmf path")
	}
}

func TestReport(t *testing.T) {
	user, _ := setupHome(t)
	now := time.Now()
	write(t, filepath.Join(user, ".claude", "projects", "p", "s.jsonl"), strings.Repeat("x", 28000))
	os.MkdirAll(filepath.Join(user, ".codex", "sessions"), 0o700)
	write(t, HookFile("claude_code", user), `{"enabledPlugins":{"cmf-capture@context-memory-fabric":true}}`)
	env := &home.Env{}
	env.Set("COMPOSE_PROFILES", "spark,capture")
	// Unset folders: the poller mounts empty ones, and turning capture on
	// would point it at the real ones.
	if s := Report(user, env, now)[0]; s.Poller || !strings.Contains(s.Note, "CMF_CAPTURE_CLAUDE_CODE_DIR isn't set") {
		t.Fatalf("%+v", s)
	}
	defaults := DefaultFolders(user, env)
	if defaults["CMF_CAPTURE_CLAUDE_CODE_DIR"] != filepath.Join(user, ".claude", "projects") || defaults["CMF_CAPTURE_CODEX_DIR"] == "" || defaults["CMF_CAPTURE_ANTIGRAVITY_DIR"] != "" {
		t.Fatal(defaults)
	}
	for k, v := range defaults {
		env.Set(k, v)
	}
	states := Report(user, env, now)
	by := map[string]SourceState{}
	for _, s := range states {
		by[s.ID] = s
	}
	if s := by["claude_code"]; s.Method != "checkpoint hook and poller (full transcript)" || s.DailyTok != 1000 || !strings.Contains(s.Note, "twice on purpose") || s.Folder == "" || s.Last.IsZero() {
		t.Fatalf("%+v", s)
	}
	if s := by["codex"]; s.Method != "poller" {
		t.Fatalf("%+v", s)
	}
	if s := by["antigravity"]; s.Method != "not on this machine" {
		t.Fatalf("%+v", s)
	}
	est := strings.Join(Estimate(states, "local"), "\n")
	if !strings.Contains(est, "1k tokens/day") || !strings.Contains(est, "$0") || !strings.Contains(est, "Checkpoint hooks") {
		t.Fatal(est)
	}
	env.Set("COMPOSE_PROFILES", "spark")
	if s := Report(user, env, now)[2]; s.Method != "server instructions only" {
		t.Fatalf("%+v", s)
	}
}

func TestBlock(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "AGENTS.md")
	write(t, path, "# Project\n\nUse recall_mem before answering.\n")
	b, err := PlanBlock(path, false)
	if err != nil || len(b.Duplicates) != 1 || !strings.Contains(b.Duplicates[0], "line 3") {
		t.Fatalf("%v %v", err, b.Duplicates)
	}
	os.WriteFile(path, b.New, 0o644)
	again, _ := PlanBlock(path, false)
	if !again.Empty() || strings.Count(string(again.New), BlockBegin) != 1 {
		t.Fatal("replaced in place, never appended twice")
	}
	gone, _ := PlanBlock(path, true)
	if strings.Contains(string(gone.New), BlockBegin) || !strings.Contains(string(gone.New), "# Project") {
		t.Fatal(string(gone.New))
	}
	write(t, path, "x\n"+BlockBegin+"\nno end\n")
	if _, err := PlanBlock(path, false); err == nil {
		t.Fatal("a broken fence must not be rewritten")
	}
}

func TestNudgeNamesTheRepositoryAsProject(t *testing.T) {
	repo := filepath.Join(t.TempDir(), "Context Memory Fabric")
	if err := os.MkdirAll(filepath.Join(repo, ".git"), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(repo, "cmd"), 0o755); err != nil {
		t.Fatal(err)
	}
	if got := ProjectHint(filepath.Join(repo, "cmd")); got != "context-memory-fabric" {
		t.Fatal(got)
	}
	if got := ProjectHint(t.TempDir()); got != "" {
		t.Fatal(got)
	}
	if got := render("codex", true, "cmf")["reason"]; got != Reason+` If you call it, pass project="cmf".` {
		t.Fatal(got)
	}
	if got := Render("codex", true)["reason"]; got != Reason {
		t.Fatal(got)
	}
}
