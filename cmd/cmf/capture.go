package main

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/capture"
	"github.com/tmargolis/context-memory-fabric/internal/home"
)

// Replaced in tests.
var hookStdin io.Reader = os.Stdin

// runHook is what an AI app's Stop hook runs (`cmf hook stop --harness X`).
// It always exits 0 and prints the harness's JSON: a broken hook must never
// trap a session.
func runHook(args []string, out io.Writer) error {
	harness := ""
	for i := 0; i < len(args); i++ {
		if args[i] == "--harness" && i+1 < len(args) {
			harness = args[i+1]
		} else if v, ok := strings.CutPrefix(args[i], "--harness="); ok {
			harness = v
		}
	}
	if len(args) == 0 || args[0] != "stop" {
		fmt.Fprintln(out, "{}")
		return nil
	}
	data, _ := io.ReadAll(io.LimitReader(hookStdin, 4<<20))
	path := os.Getenv("CMF_NUDGE_STATE_PATH")
	if path == "" {
		if h, err := home.Default(); err == nil {
			path = filepath.Join(h.Dir, "state", "capture_nudge_state.json")
		}
	}
	enc := json.NewEncoder(out)
	if path == "" {
		return enc.Encode(capture.Render(harness, false))
	}
	return enc.Encode(capture.RunHook(harness, data, path, time.Now().UTC()))
}

// promptBudget asks the running server what every session receives: its
// instructions and tool descriptions (tokens, roughly).
func promptBudget(ctx context.Context, url, token string) (instr, tools int, err error) {
	var out, log bytes.Buffer
	b := &bridge{url: url, token: token, client: &http.Client{Timeout: 10 * time.Second}, out: &out, log: &log}
	in := `{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"cmf-capture-report","version":"` + version + `"}}}` + "\n" +
		`{"jsonrpc":"2.0","method":"notifications/initialized"}` + "\n" + `{"jsonrpc":"2.0","id":2,"method":"tools/list"}` + "\n"
	b.run(ctx, strings.NewReader(in))
	for _, line := range strings.Split(strings.TrimSpace(out.String()), "\n") {
		var m struct {
			ID     int             `json:"id"`
			Result json.RawMessage `json:"result"`
			Error  *struct{ Message string }
		}
		if json.Unmarshal([]byte(line), &m) != nil {
			continue
		}
		if m.Error != nil {
			return 0, 0, errors.New(m.Error.Message)
		}
		switch m.ID {
		case 1:
			var r struct{ Instructions string }
			json.Unmarshal(m.Result, &r)
			instr = capture.Tokens(r.Instructions)
		case 2:
			tools = capture.Tokens(string(m.Result))
		}
	}
	if instr == 0 && tools == 0 {
		return 0, 0, errors.New("the server didn't answer")
	}
	return instr, tools, nil
}

func runCapture(args []string, out io.Writer) error {
	h, err := home.Default()
	if err != nil {
		return err
	}
	env, err := loadEnv(h)
	if err != nil {
		return err
	}
	user, err := userHome()
	if err != nil {
		return err
	}
	verb := ""
	if len(args) > 0 {
		verb, args = args[0], args[1:]
	}
	var dryRun, yes, remove bool
	var rest []string
	for _, a := range args {
		switch a {
		case "--dry-run":
			dryRun = true
		case "--yes", "-y":
			yes = true
		case "--remove":
			remove = true
		default:
			rest = append(rest, a)
		}
	}
	switch verb {
	case "", "--json":
		states := capture.Report(user, env, now())
		provider := env.Get("CMF_CAPTURE_LLM_PROVIDER")
		if provider == "" {
			provider = "local"
		}
		t := clientTarget(env)
		instr, tools, berr := promptBudget(context.Background(), t.LocalURL(), t.Token)
		if verb == "--json" {
			return json.NewEncoder(out).Encode(map[string]any{"sources": states, "estimate": capture.Estimate(states, provider),
				"prompt_tokens": map[string]int{"server_instructions": instr, "tool_descriptions": tools, "checkpoint": capture.Tokens(capture.Reason)}})
		}
		for _, s := range states {
			fmt.Fprintf(out, "%-29s %s\n", s.Label, s.Method)
			if s.Present {
				fmt.Fprintf(out, "%-29s %s of transcripts in the last 7 days (≈ %s tokens/day)\n", "", size(s.Week), capture.Human(s.DailyTok))
			}
			if s.Note != "" {
				fmt.Fprintf(out, "%-29s note: %s\n", "", s.Note)
			}
		}
		fmt.Fprintln(out, "\nEstimates:")
		for _, l := range capture.Estimate(states, provider) {
			fmt.Fprintln(out, "  "+l)
		}
		fmt.Fprintln(out, "\nPrompt context CMF adds to each session (approximate):")
		if berr != nil {
			fmt.Fprintf(out, "  server instructions and tool descriptions: unknown (%v)\n", berr)
		} else {
			fmt.Fprintf(out, "  server instructions ≈ %d tokens, tool descriptions ≈ %d tokens (once per session)\n", instr, tools)
		}
		fmt.Fprintf(out, "  checkpoint hook ≈ %d tokens of text each time it fires; no agent-instruction block is installed by default\n", capture.Tokens(capture.Reason))
		return nil

	case "hook":
		if len(rest) < 2 || (rest[0] != "install" && rest[0] != "remove") {
			return fmt.Errorf("usage: cmf capture hook install|remove <%s ...> [--dry-run] [--yes]", strings.Join(capture.Harnesses, "|"))
		}
		var changes []*capture.HookChange
		for _, harness := range rest[1:] {
			if !contains(capture.Harnesses, harness) {
				return fmt.Errorf("unknown app %q (one of %s)", harness, strings.Join(capture.Harnesses, ", "))
			}
			c, err := capture.PlanHook(harness, user, bridgePath(), rest[0] == "remove")
			if err != nil {
				return fmt.Errorf("%s: %w", harness, err)
			}
			fmt.Fprintf(out, "%s: %s (%s)\n", harness, c.Note, c.Path)
			if !c.Empty() {
				changes = append(changes, c)
			}
		}
		if len(changes) == 0 || dryRun {
			if dryRun && len(changes) > 0 {
				fmt.Fprintln(out, "Dry run: nothing was changed.")
			}
			return nil
		}
		if !yes && !confirm(out, "Apply?") {
			return errors.New("cancelled; nothing was changed")
		}
		backup, err := h.NewBackup(now())
		if err != nil {
			return err
		}
		backup.SaveNote("cmf capture hook " + rest[0])
		for _, c := range changes {
			if err := c.Write(backup); err != nil {
				return err
			}
			if c.Harness == "codex" && rest[0] == "install" {
				fmt.Fprintln(out, "Codex asks you to trust a new hook once: open Codex and approve it under /hooks.")
			}
		}
		fmt.Fprintf(out, "Done. Undo with: cmf restore %s\n", backup.Stamp)
		return nil

	case "block":
		if len(rest) != 1 {
			return errors.New("usage: cmf capture block <CLAUDE.md|AGENTS.md|file> [--remove] [--dry-run] [--yes]  (only for apps that get neither CMF's server instructions nor a hook)")
		}
		path, _ := filepath.Abs(rest[0])
		b, err := capture.PlanBlock(path, remove)
		if err != nil {
			return err
		}
		for _, d := range b.Duplicates {
			fmt.Fprintln(out, "Note: other CMF text in "+path+" ("+d+"); the block doesn't need it, check whether it should go")
		}
		if b.Empty() {
			fmt.Fprintln(out, "Nothing to change.")
			return nil
		}
		action := "write the CMF block (between " + capture.BlockBegin + " and " + capture.BlockEnd + ") into " + path
		if remove {
			action = "remove the CMF block from " + path
		}
		fmt.Fprintln(out, "Review: "+action+" (backup kept)")
		if dryRun {
			fmt.Fprintln(out, "Dry run: nothing was changed.")
			return nil
		}
		if !yes && !confirm(out, "Apply?") {
			return errors.New("cancelled; nothing was changed")
		}
		backup, err := h.NewBackup(now())
		if err != nil {
			return err
		}
		backup.SaveNote("cmf capture block " + path)
		if err := backup.Save(path); err != nil {
			return err
		}
		if err := home.WriteFile(path, b.New, 0o644); err != nil {
			return err
		}
		fmt.Fprintf(out, "Done. Undo with: cmf restore %s\n", backup.Stamp)
		return nil
	}
	return errors.New("usage: cmf capture [--json] | cmf capture hook install|remove <app ...> | cmf capture block <file> [--remove]")
}

func size(n int64) string {
	switch {
	case n >= 1<<30:
		return fmt.Sprintf("%.1f GB", float64(n)/(1<<30))
	case n >= 1<<20:
		return fmt.Sprintf("%.0f MB", float64(n)/(1<<20))
	}
	return fmt.Sprintf("%d KB", n/1024)
}

func contains(list []string, s string) bool {
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}
