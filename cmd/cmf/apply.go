package main

import (
	"bufio"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"time"

	contextmemoryfabric "github.com/tmargolis/context-memory-fabric"
	"github.com/tmargolis/context-memory-fabric/internal/apply"
	"github.com/tmargolis/context-memory-fabric/internal/clients"
	"github.com/tmargolis/context-memory-fabric/internal/features"
	"github.com/tmargolis/context-memory-fabric/internal/home"
	"github.com/tmargolis/context-memory-fabric/internal/mode"
	"github.com/tmargolis/context-memory-fabric/internal/probe"
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
		if s.ComingSoon {
			state, kind = "-", ", coming soon"
		} else if s.Derived {
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
	ctx := context.Background()
	rp, err := planRequest(ctx, req)
	if err != nil {
		return err
	}
	for _, w := range rp.Plan.Warnings {
		fmt.Fprintln(out, "Note:", w)
	}
	if !rp.Plan.OK() {
		fmt.Fprintln(out, "Can't apply this feature file:")
		for _, e := range rp.Plan.Errors {
			fmt.Fprintln(out, "  -", e)
		}
		return errors.New("nothing was changed")
	}
	if rp.Empty() {
		fmt.Fprintln(out, "Nothing to change: the machine already matches the feature file.")
		return nil
	}
	fmt.Fprintln(out, "Review changes:")
	for _, line := range rp.Review() {
		fmt.Fprintln(out, line)
	}
	if dryRun {
		fmt.Fprintln(out, "Dry run: nothing was changed.")
		return nil
	}
	if err := rp.Ready(); err != nil {
		return err
	}
	if !yes && !confirm(out, "Apply?") {
		return errors.New("cancelled; nothing was changed")
	}
	fmt.Fprintln(out, "Applying:")
	backup, err := rp.Apply(ctx, out)
	if err != nil {
		return err
	}
	fmt.Fprintf(out, "Done. Undo with: cmf restore %s\n", backup.Stamp)
	return nil
}

// requestPlan is a feature request worked out against this machine: the
// .env plan and the AI app writes. The CLI and the UI review and apply it
// the same way.
type requestPlan struct {
	Home     home.Home
	Manifest *features.Manifest
	Plan     *features.Plan
	Writes   []apply.ClientWrite
	Facts    probe.Facts
}

func planRequest(ctx context.Context, req *features.Request) (*requestPlan, error) {
	h, err := home.Default()
	if err != nil {
		return nil, err
	}
	m, err := features.Load()
	if err != nil {
		return nil, err
	}
	env, err := loadEnv(h)
	if err != nil {
		return nil, err
	}
	facts, err := collect(ctx)
	if err != nil {
		return nil, err
	}
	pctx := features.Context{Nodes: facts.Nodes, Home: h.Dir}
	if user, err := userHome(); err == nil {
		pctx.User = user
	}
	if state, err := mode.Load(h); err == nil && state.Mode.Mode == mode.Developer {
		pctx.Repo = state.Mode.Repo
	}
	rp := &requestPlan{Home: h, Manifest: m, Plan: m.Plan(env, req, pctx), Facts: facts}
	// AI app entries (item 3.4) reach the server the new .env describes.
	for id := range req.Clients {
		if _, ok := clients.Find(id); !ok {
			rp.Plan.Errors = append(rp.Plan.Errors, fmt.Sprintf("unknown AI app %q under \"clients\" (one of %s)", id, strings.Join(clients.IDs(), ", ")))
		}
	}
	if rp.Plan.OK() && len(req.Clients) > 0 {
		if rp.Writes, err = planClientWrites(req.Clients, clientTarget(rp.Plan.Env), clientEnv()); err != nil {
			rp.Plan.Errors = append(rp.Plan.Errors, err.Error())
		}
	}
	return rp, nil
}

func (rp *requestPlan) Empty() bool { return rp.Plan.Empty() && len(rp.Writes) == 0 }

// Review is the plain-language list of what Apply will do.
func (rp *requestPlan) Review() []string {
	var lines []string
	if summary := describe(rp.Plan); summary != "" && !rp.Plan.Empty() {
		lines = append(lines, "  "+summary)
	}
	var b strings.Builder
	printClientWrites(&b, rp.Writes)
	if s := strings.TrimRight(b.String(), "\n"); s != "" {
		lines = append(lines, strings.Split(s, "\n")...)
	}
	if rp.Plan.StarterWiki != "" {
		lines = append(lines, fmt.Sprintf("  Write the demo wiki's notes into %s (notes already there are kept)", rp.Plan.StarterWiki))
	}
	if !rp.Plan.Empty() {
		lines = append(lines, fmt.Sprintf("  Write %s (backup kept):", rp.Home.EnvFile()))
		for _, c := range rp.Plan.Changes {
			lines = append(lines, "    - "+c.String())
		}
		for _, svc := range rp.Plan.Stop {
			lines = append(lines, fmt.Sprintf("  Stop the %s container", svc))
		}
		lines = append(lines, "  Recreate the containers whose settings changed (docker compose up -d), then wait for the server")
	}
	for _, n := range rp.Plan.Notes {
		lines = append(lines, "  Cost: "+n)
	}
	return lines
}

// Ready says why Apply can't run now, if it can't.
func (rp *requestPlan) Ready() error {
	if !rp.Plan.Empty() && !rp.Facts.Docker.Responding {
		return errors.New("Docker isn't answering, so the change can't be checked; run `cmf doctor` first")
	}
	return nil
}

func (rp *requestPlan) Apply(ctx context.Context, out io.Writer) (*home.Backup, error) {
	if rp.Plan.StarterWiki != "" {
		if err := writeStarterWiki(rp.Plan.StarterWiki); err != nil {
			return nil, fmt.Errorf("writing the demo wiki: %w", err)
		}
	}
	backup, err := executor(rp.Home, rp.Manifest, rp.Facts.Port, out).Apply(ctx, rp.Plan, describe(rp.Plan), rp.Writes...)
	if err != nil {
		return backup, err
	}
	afterNotes(out, rp.Writes)
	return backup, nil
}

// writeStarterWiki writes the demo wiki carried in the binary into dir,
// keeping any note already there (the user may have edited it).
func writeStarterWiki(dir string) error {
	root, err := fs.Sub(contextmemoryfabric.StarterWiki, "starter-wiki")
	if err != nil {
		return err
	}
	return fs.WalkDir(root, ".", func(path string, d fs.DirEntry, err error) error {
		if err != nil || d.IsDir() {
			return err
		}
		dst := filepath.Join(dir, filepath.FromSlash(path))
		if _, err := os.Stat(dst); err == nil {
			return nil
		}
		data, err := fs.ReadFile(root, path)
		if err != nil {
			return err
		}
		return home.WriteFile(dst, data, 0o644)
	})
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
