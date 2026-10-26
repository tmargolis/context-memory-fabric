package main

import (
	"errors"
	"fmt"
	"io"
	"slices"
	"strings"

	contextmemoryfabric "github.com/tmargolis/context-memory-fabric"
	"github.com/tmargolis/context-memory-fabric/internal/capture"
	"github.com/tmargolis/context-memory-fabric/internal/home"
)

var instructionApps = []string{"claude-code", "codex", "antigravity"}

// runInstructions: `cmf instructions add|remove <app>` writes CMF's
// instruction block into an app's own instructions file (Claude Code's
// CLAUDE.md, Codex's AGENTS.md, Antigravity's ~/.gemini/config/AGENTS.md),
// between cmf:begin/end markers, backup kept (v0.1.8). Antigravity has no
// editor for global rules, so a button beats "create this file".
func runInstructions(args []string, out io.Writer) error {
	var dryRun, yes bool
	var rest []string
	for _, a := range args {
		switch a {
		case "--dry-run":
			dryRun = true
		case "--yes", "-y":
			yes = true
		default:
			rest = append(rest, a)
		}
	}
	if len(rest) != 2 || (rest[0] != "add" && rest[0] != "remove") || !slices.Contains(instructionApps, rest[1]) {
		return fmt.Errorf("usage: cmf instructions add|remove <%s> [--dry-run] [--yes]", strings.Join(instructionApps, "|"))
	}
	remove, app := rest[0] == "remove", rest[1]
	cenv := clientEnv()
	path := capture.InstructionsPath(app, cenv.Home, cenv.Getenv)
	if !remove {
		if f := capture.CheckInstructions(cenv.Home, cenv.Getenv)[app]; f.State == "current" {
			fmt.Fprintln(out, path+" already has CMF's instructions; nothing to change.")
			return nil
		}
	}
	b, err := capture.PlanBlockText(path, contextmemoryfabric.AgentInstructions(), remove)
	if err != nil {
		return err
	}
	if b.Empty() {
		fmt.Fprintln(out, "Nothing to change.")
		return nil
	}
	action := "add CMF's instructions to " + path + " (between " + capture.BlockBegin + " and " + capture.BlockEnd + ")"
	if remove {
		action = "remove CMF's instructions block from " + path
	}
	fmt.Fprintln(out, "Review: "+action+" (backup kept)")
	for _, d := range b.Duplicates {
		fmt.Fprintln(out, "Note: older CMF text outside the markers ("+d+"); check whether it should go")
	}
	if dryRun {
		fmt.Fprintln(out, "Dry run: nothing was changed.")
		return nil
	}
	if !yes && !confirm(out, "Apply?") {
		return errors.New("cancelled; nothing was changed")
	}
	h, err := home.Default()
	if err != nil {
		return err
	}
	backup, err := h.NewBackup(now())
	if err != nil {
		return err
	}
	backup.SaveNote("cmf instructions " + rest[0] + " " + app)
	if err := backup.Save(path); err != nil {
		return err
	}
	if err := home.WriteFile(path, b.New, 0o644); err != nil {
		return err
	}
	fmt.Fprintf(out, "Done. Start a new session in the app to pick them up. Undo with: cmf restore %s\n", backup.Stamp)
	return nil
}
