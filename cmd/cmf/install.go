package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"

	"github.com/tmargolis/context-memory-fabric/internal/capture"
	"github.com/tmargolis/context-memory-fabric/internal/home"
	"github.com/tmargolis/context-memory-fabric/internal/migrate"
	"github.com/tmargolis/context-memory-fabric/internal/mode"
)

// runInstall creates a fresh Standard-mode install in ~/.cmf (plan section
// 3.2): the stack's compose file, mode.json and a starting .env. It starts
// nothing: the server needs a model provider and its key first, which is
// what Features → Apply sets.
func runInstall(args []string, out io.Writer) error {
	var yes, dryRun bool
	for _, a := range args {
		switch a {
		case "--yes", "-y":
			yes = true
		case "--dry-run":
			dryRun = true
		default:
			return errors.New("usage: cmf install [--dry-run] [--yes]")
		}
	}
	h, err := home.Default()
	if err != nil {
		return err
	}
	if home.Exists(h.EnvFile()) {
		return fmt.Errorf("CMF is already installed in %s; `cmf status` checks it and `cmf mode` shows how it runs", h.Dir)
	}
	if p, err := migratePaths(""); err == nil {
		if s := migrate.Look(context.Background(), p, migrateOps(io.Discard)); s.Migratable(p) {
			return fmt.Errorf("found an older CMF install (%s, from %s); `cmf migrate` moves it here with its memories instead", s.Kind, s.Repo)
		}
	}
	changes, err := mode.Plan(h, mode.Mode{Mode: mode.Standard})
	if err != nil {
		return err
	}
	if changes, err = mode.PinImage(h, changes, version); err != nil {
		return err
	}
	env := &home.Env{}
	for _, c := range changes { // PinImage's .env change, if any, is the base
		if c.Path == h.EnvFile() {
			if e, err := home.ParseEnv(c.Data); err == nil {
				env = e
			}
		}
	}
	env.Set("FALKORDB_DATABASE", "cmf")    // one graph per install, never asked (section 3.4)
	env.Set("COMPOSE_PROFILES", "capture") // capture on by default (section 3.9)
	if user, err := userHome(); err == nil {
		for key, dir := range capture.DefaultFolders(user, env) { // the poller mounts these
			env.Set(key, dir)
		}
	}
	var kept []mode.Change
	for _, c := range changes {
		if c.Path != h.EnvFile() {
			kept = append(kept, c)
		}
	}
	changes = append(kept, mode.Change{Path: h.EnvFile(), Note: "write a starting .env (graph `cmf`, capture on" + map[bool]string{true: ", image " + env.Get("CMF_VERSION"), false: ""}[env.Get("CMF_VERSION") != ""] + ")", Data: env.Bytes(), Perm: 0o600})

	fmt.Fprintf(out, "Install CMF in %s (Standard mode, the published server image):\n", h.Dir)
	for _, c := range changes {
		fmt.Fprintf(out, "  - %s: %s\n", c.Path, c.Note)
	}
	if dryRun {
		fmt.Fprintln(out, "Dry run: nothing written.")
		return nil
	}
	if !yes && !confirm(out, "Install?") {
		return errors.New("cancelled; nothing was written")
	}
	if err := os.MkdirAll(filepath.Join(h.Dir, "state"), 0o700); err != nil {
		return err
	}
	if _, err := mode.Apply(h, changes, now()); err != nil {
		return err
	}
	fmt.Fprintln(out, "Installed. Next: choose a model provider and its key under Features (`cmf` opens CMF management), then Apply; that starts the stack.")
	return nil
}
