package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/capture"
	"github.com/tmargolis/context-memory-fabric/internal/home"
	"github.com/tmargolis/context-memory-fabric/internal/run"
)

// Each adapter's backfill flag for "every project / app folder", as its CLI names it.
var backfillAll = map[string][]string{
	"claude_code":   {"--all-projects"},
	"claude_cowork": nil,
	"codex":         {"--all"},
	"antigravity":   {"--all-app-dirs"},
}

// BackfillRecord is state/backfill.json: the first-run offer is made once.
type BackfillRecord struct {
	At      time.Time `json:"at"`
	Days    int       `json:"days"`
	Sources []string  `json:"sources"`
	Result  string    `json:"result"` // "done", "failed" or "declined"
}

func backfillRecordPath(h home.Home) string { return filepath.Join(h.Dir, "state", "backfill.json") }

func backfillCmd(h home.Home, source string, since time.Time) run.Cmd {
	args := []string{"compose", "--project-directory", h.Dir, "exec", "-T", "cmf-poller", "python", "-m", "server.adapters." + source + ".cli", "backfill"}
	args = append(args, backfillAll[source]...)
	return run.Cmd{Name: "docker", Args: append(args, "--since", since.UTC().Format(time.RFC3339))}
}

// runBackfill imports the last N days of transcripts through the poller
// container (item 4.1): estimate first, then each source with live output.
func runBackfill(args []string, out io.Writer) error {
	days := 14
	var only []string
	var dryRun, yes, decline bool
	for i := 0; i < len(args); i++ {
		switch a := args[i]; {
		case a == "--dry-run":
			dryRun = true
		case a == "--yes" || a == "-y":
			yes = true
		case a == "--decline":
			decline = true
		case (a == "--days" || a == "--source") && i+1 < len(args):
			i++
			if a == "--days" {
				n, err := strconv.Atoi(args[i])
				if err != nil || n < 1 || n > 365 {
					return fmt.Errorf("--days takes 1 to 365, got %q", args[i])
				}
				days = n
			} else {
				only = append(only, strings.Split(args[i], ",")...)
			}
		default:
			return errors.New("usage: cmf backfill [--days N] [--source claude_code,codex,...] [--dry-run] [--yes] | cmf backfill --decline")
		}
	}
	h, err := home.Default()
	if err != nil {
		return err
	}
	if decline {
		return saveBackfill(h, BackfillRecord{At: now(), Result: "declined"})
	}
	env, err := loadEnv(h)
	if err != nil {
		return err
	}
	user, err := userHome()
	if err != nil {
		return err
	}
	for _, s := range only {
		if _, ok := backfillAll[s]; !ok {
			return fmt.Errorf("unknown source %q (one of claude_code, claude_cowork, codex, antigravity)", s)
		}
	}
	since := now().Add(-time.Duration(days) * 24 * time.Hour)
	provider := capture.LLM(env)

	var chosen []string
	var total int64
	fmt.Fprintf(out, "Backfill the last %d days (since %s) into the review queue:\n", days, since.Format("2006-01-02"))
	for _, s := range capture.Report(user, env, now()) {
		if len(only) > 0 && !contains(only, s.ID) || !s.Present {
			continue
		}
		var bytes int64
		for _, d := range s.Dirs {
			bytes += capture.WindowBytes(filepath.Join(user, d), since)
		}
		total += bytes / 4
		chosen = append(chosen, s.ID)
		fmt.Fprintf(out, "  %-29s %s of transcripts (≈ %s tokens at most)\n", s.Label, size(bytes), capture.Human(bytes/4))
	}
	if len(chosen) == 0 {
		fmt.Fprintln(out, "  no transcripts on this machine for those sources")
		return nil
	}
	cost := "$0 on the local model; it takes time (the Spark lock queues it behind other extraction)"
	if provider != "local" {
		cost = "priced by " + provider + "'s per-token rate; " + capture.ToFree
	}
	fmt.Fprintf(out, "Estimate: at most ≈ %s tokens through the capture LLM (%s). Everything lands in the review queue; nothing reaches the graph until it's approved.\n", capture.Human(total), cost)
	if dryRun {
		fmt.Fprintln(out, "Dry run: nothing was run.")
		return nil
	}
	facts, err := collect(context.Background())
	if err != nil {
		return err
	}
	if p := facts.Service("cmf-poller"); p == nil || p.State != "running" {
		return errors.New("the cmf-poller container isn't running; turn capture on (Features) or `cmf doctor --fix=capture` first")
	}
	if !yes && !confirm(out, "Start the backfill?") {
		return errors.New("cancelled; nothing was run")
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	rec := BackfillRecord{At: now(), Days: days, Sources: chosen, Result: "done"}
	for i, s := range chosen {
		fmt.Fprintf(out, "\n[%d/%d] %s\n", i+1, len(chosen), s)
		cmd := backfillCmd(h, s, since)
		cmd.Stream = out
		if res := runner.Run(ctx, cmd); res.Err != nil {
			rec.Result = "failed"
			saveBackfill(h, rec)
			return fmt.Errorf("%s backfill: %v (the sources before it are done; rerun with --source for the rest)", s, res.Err)
		}
	}
	fmt.Fprintln(out, "\nDone. Review what it found with your AI app (\"show me CMF's review queue\") or the nightly review.")
	return saveBackfill(h, rec)
}

func saveBackfill(h home.Home, rec BackfillRecord) error {
	data, _ := json.MarshalIndent(rec, "", "  ")
	return home.WriteFile(backfillRecordPath(h), append(data, '\n'), 0o600)
}
