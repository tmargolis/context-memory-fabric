package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"strings"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/apply"
	"github.com/tmargolis/context-memory-fabric/internal/features"
	"github.com/tmargolis/context-memory-fabric/internal/home"
	"github.com/tmargolis/context-memory-fabric/internal/mode"
)

// Replaced in tests.
var (
	stdin         io.Reader = os.Stdin
	sleep                   = time.Sleep
	healthTimeout           = 3 * time.Minute
)

func loadEnv(h home.Home) (*home.Env, error) {
	env, err := home.ReadEnv(h.EnvFile())
	if errors.Is(err, os.ErrNotExist) {
		return &home.Env{}, nil
	}
	return env, err
}

func runFeatures(args []string, out io.Writer) error {
	asJSON := len(args) == 1 && args[0] == "--json"
	if len(args) > 0 && !asJSON {
		return errors.New("usage: cmf features [--json]")
	}
	h, err := home.Default()
	if err != nil {
		return err
	}
	m, err := features.Load()
	if err != nil {
		return err
	}
	env, err := loadEnv(h)
	if err != nil {
		return err
	}
	states := m.States(env)
	if asJSON {
		enc := json.NewEncoder(out)
		enc.SetIndent("", "  ")
		return enc.Encode(map[string]any{"features": states})
	}
	group := ""
	for _, s := range states {
		if s.Group != group {
			group = s.Group
			fmt.Fprintf(out, "\n%s\n", strings.ToUpper(group))
		}
		state := "off"
		if s.Enabled {
			state = "on"
		}
		kind := ""
		if s.Derived {
			kind = ", follows the others"
		} else if s.AlwaysOn {
			kind = ", always"
		}
		fmt.Fprintf(out, "  [%-3s] %-14s %s%s\n", state, s.ID, s.Label, kind)
		for _, in := range s.Inputs {
			value := in.Value
			if value == "" {
				value = "(not set)"
			} else if !in.IsSet {
				value += " (default)"
			}
			fmt.Fprintf(out, "          %-20s %s  [%s]\n", in.ID, value, in.Env)
		}
	}
	fmt.Fprintln(out, "\nTo change: write a feature file and run `cmf apply <file>`; e.g.")
	fmt.Fprintln(out, `  {"features": {"notion": {"enabled": true, "token": "…"}, "capture": {"enabled": false}}}`)
	return nil
}

func confirm(out io.Writer, question string) bool {
	fmt.Fprintf(out, "%s [y/N] ", question)
	line, _ := bufio.NewReader(stdin).ReadString('\n')
	answer := strings.ToLower(strings.TrimSpace(line))
	return answer == "y" || answer == "yes"
}

func executor(h home.Home, m *features.Manifest, port int, out io.Writer) apply.Executor {
	return apply.Executor{Home: h, Manifest: m, Runner: runner, Get: getHealth, Port: port,
		Now: now, Sleep: sleep, HealthTimeout: healthTimeout, Log: func(s string) { fmt.Fprintln(out, s) }}
}

func runApply(args []string, out io.Writer) error {
	var file string
	var dryRun, yes bool
	for _, a := range args {
		switch {
		case a == "--dry-run":
			dryRun = true
		case a == "--yes" || a == "-y":
			yes = true
		case strings.HasPrefix(a, "-"):
			return fmt.Errorf("unknown flag %s", a)
		case file == "":
			file = a
		default:
			return errors.New("one feature file at a time")
		}
	}
	if file == "" {
		return errors.New("usage: cmf apply <feature-file.json> [--dry-run] [--yes]  (see `cmf features`)")
	}
	data, err := os.ReadFile(file)
	if err != nil {
		return err
	}
	req, err := features.ParseRequest(data)
	if err != nil {
		return err
	}
	h, err := home.Default()
	if err != nil {
		return err
	}
	m, err := features.Load()
	if err != nil {
		return err
	}
	env, err := loadEnv(h)
	if err != nil {
		return err
	}
	ctx := context.Background()
	facts, err := collect(ctx)
	if err != nil {
		return err
	}
	pctx := features.Context{Nodes: facts.Nodes}
	if state, err := mode.Load(h); err == nil && state.Mode.Mode == mode.Developer {
		pctx.Repo = state.Mode.Repo
	}

	plan := m.Plan(env, req, pctx)
	for _, w := range plan.Warnings {
		fmt.Fprintln(out, "Note:", w)
	}
	if !plan.OK() {
		fmt.Fprintln(out, "Can't apply this feature file:")
		for _, e := range plan.Errors {
			fmt.Fprintln(out, "  -", e)
		}
		return errors.New("nothing was changed")
	}
	if plan.Empty() {
		fmt.Fprintln(out, "Nothing to change: the machine already matches the feature file.")
		return nil
	}
	summary := describe(plan)
	fmt.Fprintln(out, "Review changes:")
	if summary != "" {
		fmt.Fprintln(out, "  "+summary)
	}
	fmt.Fprintf(out, "  Write %s (backup kept):\n", h.EnvFile())
	for _, c := range plan.Changes {
		fmt.Fprintln(out, "    -", c)
	}
	for _, svc := range plan.Stop {
		fmt.Fprintf(out, "  Stop the %s container\n", svc)
	}
	fmt.Fprintln(out, "  Recreate the containers whose settings changed (docker compose up -d), then wait for the server")
	for _, n := range plan.Notes {
		fmt.Fprintln(out, "  Cost: "+n)
	}
	if dryRun {
		fmt.Fprintln(out, "Dry run: nothing was changed.")
		return nil
	}
	if !facts.Docker.Responding {
		return errors.New("Docker isn't answering, so the change can't be checked; run `cmf doctor` first")
	}
	if !yes && !confirm(out, "Apply?") {
		return errors.New("cancelled; nothing was changed")
	}
	fmt.Fprintln(out, "Applying:")
	backup, err := executor(h, m, facts.Port, out).Apply(ctx, plan, summary)
	if err != nil {
		return err
	}
	fmt.Fprintf(out, "Done. Undo with: cmf restore %s\n", backup.Stamp)
	return nil
}

func describe(p *features.Plan) string {
	var parts []string
	if len(p.Enabled) > 0 {
		parts = append(parts, "turn on "+strings.Join(p.Enabled, ", "))
	}
	if len(p.Disabled) > 0 {
		parts = append(parts, "turn off "+strings.Join(p.Disabled, ", "))
	}
	if len(parts) == 0 {
		var keys []string
		for _, c := range p.Changes {
			keys = append(keys, c.Key)
		}
		parts = append(parts, "change "+strings.Join(keys, ", "))
	}
	return strings.Join(parts, "; ")
}

func runRestore(args []string, out io.Writer) error {
	var stamp string
	var yes bool
	for _, a := range args {
		switch {
		case a == "--yes" || a == "-y":
			yes = true
		case strings.HasPrefix(a, "-"):
			return fmt.Errorf("unknown flag %s", a)
		default:
			stamp = a
		}
	}
	h, err := home.Default()
	if err != nil {
		return err
	}
	if stamp == "" {
		backups, err := h.Backups()
		if err != nil {
			return err
		}
		if len(backups) == 0 {
			fmt.Fprintln(out, "No backups in", h.BackupsDir())
			return nil
		}
		fmt.Fprintln(out, "Backups (newest first); restore one with `cmf restore <stamp>`:")
		for _, b := range backups {
			desc := b.Note
			switch b.Kind {
			case "consolidate":
				desc = "consolidation (scripts/consolidate_install.py restore " + b.Stamp + ")"
			case "unknown":
				desc = "nothing cmf can restore"
			}
			if desc == "" {
				desc = fmt.Sprintf("%d file(s)", len(b.Files))
			}
			fmt.Fprintf(out, "  %s  %s\n", b.Stamp, desc)
		}
		return nil
	}
	info, err := h.Backup(stamp)
	if err != nil {
		return err
	}
	if info.Kind != "cmf" {
		_, err := h.Restore(stamp, false, now()) // returns the explanation
		return err
	}
	fmt.Fprintf(out, "Restore %s", stamp)
	if info.Note != "" {
		fmt.Fprintf(out, " (%s)", info.Note)
	}
	fmt.Fprintln(out, ":")
	for _, f := range info.Files {
		fmt.Fprintln(out, "  -", f)
	}
	fmt.Fprintln(out, "  The current files are backed up first; then the stack is brought in line.")
	if !yes && !confirm(out, "Restore?") {
		return errors.New("cancelled; nothing was changed")
	}
	m, err := features.Load()
	if err != nil {
		return err
	}
	facts, err := collect(context.Background())
	if err != nil {
		return err
	}
	undo, err := executor(h, m, facts.Port, out).Restore(context.Background(), stamp)
	if undo != nil {
		fmt.Fprintf(out, "The files from before the restore are in backup %s.\n", undo.Stamp)
	}
	if err != nil {
		return err
	}
	fmt.Fprintln(out, "Done.")
	return nil
}
