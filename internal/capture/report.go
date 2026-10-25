package capture

import (
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/home"
)

// Source is one AI app CMF can capture from, as the poller names it.
type Source struct {
	ID      string   `json:"id"`
	Label   string   `json:"label"`
	Harness string   `json:"harness,omitempty"` // whose Stop hook nudges it ("" = no hook system on this machine)
	Dirs    []string `json:"dirs"`              // transcript roots, relative to home
}

var Sources = []Source{
	{ID: "claude_code", Label: "Claude Code and the Code tab", Harness: "claude_code", Dirs: []string{".claude/projects"}},
	{ID: "claude_cowork", Label: "Cowork", Harness: "claude_code", Dirs: []string{"Library/Application Support/Claude/local-agent-mode-sessions"}},
	{ID: "codex", Label: "Codex", Harness: "codex", Dirs: []string{".codex/sessions"}},
	{ID: "antigravity", Label: "Antigravity", Harness: "antigravity", Dirs: []string{".gemini/antigravity"}},
}

// SourceState is how one source is captured on this machine.
type SourceState struct {
	Source
	Present  bool   `json:"present"`          // transcripts on disk
	Hook     string `json:"hook,omitempty"`   // nudge kind, see Hooks.Nudge
	Worker   bool   `json:"worker,omitempty"` // a legacy host worker hook
	Poller   bool   `json:"poller"`           // the cmf-poller container reads it
	Method   string `json:"method"`           // plain words
	Week     int64  `json:"week_bytes"`       // transcript bytes written in the last 7 days
	DailyTok int64  `json:"daily_tokens"`     // ≈ Week/4/7
	Note     string `json:"note,omitempty"`
}

// PollerSources is the poller's set: COMPOSE_PROFILES must include capture;
// CMF_CAPTURE_SOURCES is "auto" (every source on disk) or a list.
func PollerSources(env *home.Env) map[string]bool {
	out := map[string]bool{}
	on := false
	for _, p := range strings.Split(env.Get("COMPOSE_PROFILES"), ",") {
		on = on || strings.TrimSpace(p) == "capture"
	}
	if !on {
		return out
	}
	spec := strings.ToLower(strings.TrimSpace(env.Get("CMF_CAPTURE_SOURCES")))
	for _, s := range Sources {
		out[s.ID] = spec == "" || spec == "auto"
	}
	if spec != "" && spec != "auto" {
		for _, n := range strings.Split(spec, ",") {
			out[strings.TrimSpace(n)] = true
		}
	}
	return out
}

// weekBytes sums the transcript files changed in the last 7 days.
func weekBytes(root string, now time.Time) int64 {
	return WindowBytes(root, now.Add(-7*24*time.Hour))
}

// WindowBytes sums the transcript files under root changed since cutoff.
func WindowBytes(root string, cutoff time.Time) int64 {
	var total int64
	filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return nil
		}
		if strings.HasSuffix(path, ".jsonl") || strings.HasSuffix(path, ".json") || strings.HasSuffix(path, ".pb") {
			if info, err := d.Info(); err == nil && info.ModTime().After(cutoff) {
				total += info.Size()
			}
		}
		return nil
	})
	return total
}

// Report reads how each source is captured. Read-only.
func Report(userHome string, env *home.Env, now time.Time) []SourceState {
	poller := PollerSources(env)
	hooks := map[string]Hooks{}
	for _, h := range Harnesses {
		hooks[h] = ReadHooks(h, userHome)
	}
	var out []SourceState
	for _, s := range Sources {
		st := SourceState{Source: s, Poller: poller[s.ID]}
		for _, d := range s.Dirs {
			root := filepath.Join(userHome, d)
			if _, err := os.Stat(root); err == nil {
				st.Present = true
				st.Week += weekBytes(root, now)
			}
		}
		st.DailyTok = st.Week / 4 / 7
		if h, ok := hooks[s.Harness]; ok {
			st.Hook, st.Worker = h.Nudge, h.Worker
		}
		if s.ID == "claude_cowork" && st.Hook != "plugin" {
			st.Hook = "" // only the plugin reaches Cowork sessions
		}
		st.Poller = st.Poller && st.Present
		switch {
		case st.Hook != "" && st.Poller:
			st.Method = "checkpoint hook and poller (full transcript)"
			st.Note = "captured twice on purpose (Advanced: full transcript); turn the poller off for this source to keep only the checkpoints"
		case st.Hook != "":
			st.Method = "checkpoint hook"
		case st.Poller:
			st.Method = "poller"
		case st.Present:
			st.Method = "server instructions only"
			st.Note = "no hook and no poller: capture depends on the app following CMF's instructions"
		default:
			st.Method = "not on this machine"
		}
		if st.Worker {
			st.Note = strings.TrimPrefix(st.Note+"; legacy host worker hook installed (the poller container does its job)", "; ")
		}
		out = append(out, st)
	}
	return out
}

// Estimate says what live capture costs, in tokens: the poller sends each
// source's new transcript text to CMF's capture LLM; a checkpoint hook costs
// the app a few hundred tokens per checkpoint instead.
func Estimate(states []SourceState, captureProvider string) []string {
	var lines []string
	var pollTok int64
	for _, s := range states {
		if s.Poller {
			pollTok += s.DailyTok
		}
	}
	if pollTok > 0 {
		cost := "$0 (a local model; it costs time, not money)"
		if captureProvider != "local" {
			cost = "priced by " + captureProvider + "'s per-token rate (set CMF_CAPTURE_LLM_PROVIDER=local for $0)"
		}
		lines = append(lines, fmt.Sprintf("Poller: at most about %s tokens/day (all transcript text of the last 7 days; it extracts from only part of it), %s.", Human(pollTok), cost))
	}
	hooks := 0
	for _, s := range states {
		if s.Hook != "" {
			hooks++
		}
	}
	if hooks > 0 {
		lines = append(lines, fmt.Sprintf("Checkpoint hooks: about %d tokens of checkpoint text each time, plus the app's capture reply (at most one per 5 turns or 30 minutes), on %d source(s).", Tokens(Reason), hooks))
	}
	return lines
}

// Tokens is a rough count (4 characters per token).
func Tokens(s string) int { return (len(s) + 3) / 4 }

// Human writes a token count compactly (12k, 2.9M).
func Human(n int64) string {
	switch {
	case n >= 1_000_000:
		return fmt.Sprintf("%.1fM", float64(n)/1e6)
	case n >= 1000:
		return fmt.Sprintf("%dk", n/1000)
	}
	return fmt.Sprint(n)
}

// --- agent block (only for apps with neither server instructions nor hooks) ----------

const (
	BlockBegin = "<!-- cmf:begin -->"
	BlockEnd   = "<!-- cmf:end -->"
	// Kept short: the server instructions are the single source of "when to
	// use CMF"; this only points the agent at them.
	BlockText = "Context Memory Fabric (CMF) is connected as an MCP server. Follow its server instructions: recall with `get_context` / `recall_mem` before answering from memory, and record decisions and durable knowledge with `capture_session` at natural checkpoints."
)

var otherCMF = regexp.MustCompile(`(?i)context memory fabric|capture_session|recall_mem|\bcmf\b`)

// Block is the result of planning an agent block for one file.
type Block struct {
	Path       string
	Old, New   []byte
	Duplicates []string // other CMF text outside the markers, flagged not removed
}

// PlanBlock puts the fenced block in path (replacing a fenced one in place), or removes it.
func PlanBlock(path string, remove bool) (*Block, error) {
	b := &Block{Path: path}
	data, err := os.ReadFile(path)
	if err != nil && !os.IsNotExist(err) {
		return nil, err
	}
	b.Old = data
	text := string(data)
	before, rest, fenced := strings.Cut(text, BlockBegin)
	after := ""
	if fenced {
		_, tail, ok := strings.Cut(rest, BlockEnd)
		if !ok {
			return nil, fmt.Errorf("%s has %s without %s; fix the markers by hand first", path, BlockBegin, BlockEnd)
		}
		after = tail
	} else {
		before = text
	}
	for i, line := range strings.Split(before+after, "\n") {
		if otherCMF.MatchString(line) {
			b.Duplicates = append(b.Duplicates, fmt.Sprintf("line %d: %s", i+1, strings.TrimSpace(line)))
		}
	}
	block := BlockBegin + "\n" + BlockText + "\n" + BlockEnd
	switch {
	case remove && fenced:
		b.New = []byte(strings.TrimRight(before, "\n") + "\n" + strings.TrimLeft(after, "\n"))
		if strings.TrimSpace(string(b.New)) == "" {
			b.New = []byte{}
		}
	case remove:
		b.New = b.Old
	case fenced:
		b.New = []byte(before + block + after)
	default:
		sep := ""
		if len(strings.TrimSpace(text)) > 0 {
			sep = strings.TrimRight(text, "\n") + "\n\n"
		}
		b.New = []byte(sep + block + "\n")
	}
	return b, nil
}

func (b *Block) Empty() bool { return string(b.Old) == string(b.New) }
