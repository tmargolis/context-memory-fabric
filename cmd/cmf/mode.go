package main

import (
	"errors"
	"flag"
	"fmt"
	"io"
	"path/filepath"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/home"
	"github.com/tmargolis/context-memory-fabric/internal/mode"
)

// now is replaced in tests.
var now = time.Now

func runMode(args []string, out io.Writer) error {
	h, err := home.Default()
	if err != nil {
		return err
	}
	if len(args) == 0 {
		return showMode(h, out)
	}
	if args[0] != "set" {
		return fmt.Errorf("usage: cmf mode | cmf mode set standard | cmf mode set developer --repo <checkout> [--dry-run]")
	}
	return setMode(h, args[1:], out)
}

func showMode(h home.Home, out io.Writer) error {
	state, err := mode.Load(h)
	if err != nil {
		return err
	}
	fmt.Fprintf(out, "Mode: %s (from %s)\n", state.Mode.Mode, state.Source)
	if state.Mode.Mode == mode.Developer {
		fmt.Fprintf(out, "Repo: %s\n", state.Mode.Repo)
	}
	fmt.Fprintf(out, "Home: %s\n", h.Dir)
	if !state.Consistent() {
		fmt.Fprintf(out, "Warning: the files say %s", state.Detected.Mode)
		if state.Detected.Repo != "" {
			fmt.Fprintf(out, " (CMF_REPO=%s)", state.Detected.Repo)
		}
		fmt.Fprintln(out, "; run `cmf mode set` to make them agree.")
	}
	return nil
}

func setMode(h home.Home, args []string, out io.Writer) error {
	if len(args) == 0 {
		return errors.New("which mode? standard or developer")
	}
	target := mode.Mode{Mode: args[0]}
	flags := flag.NewFlagSet("mode set", flag.ContinueOnError)
	flags.SetOutput(out)
	repo := flags.String("repo", "", "developer mode: absolute path to the CMF checkout")
	dryRun := flags.Bool("dry-run", false, "list the changes without writing anything")
	if err := flags.Parse(args[1:]); err != nil {
		return err
	}
	if *repo != "" {
		abs, err := filepath.Abs(*repo)
		if err != nil {
			return err
		}
		target.Repo = abs
	}
	if target.Mode == mode.Developer && target.Repo == "" {
		// Keep the checkout already configured, if there is one.
		target.Repo = mode.Detect(h).Repo
	}
	changes, err := mode.Plan(h, target)
	if err != nil {
		return err
	}
	if target.Mode == mode.Standard {
		if changes, err = mode.PinImage(h, changes, version); err != nil {
			return err
		}
	}
	if len(changes) == 0 {
		fmt.Fprintln(out, "Nothing to change.")
		return nil
	}
	fmt.Fprintln(out, "Changes:")
	for _, c := range changes {
		fmt.Fprintf(out, "  - %s: %s\n", c.Path, c.Note)
	}
	if *dryRun {
		fmt.Fprintln(out, "Dry run: nothing written.")
		return nil
	}
	backup, err := mode.Apply(h, changes, now())
	if err != nil {
		if backup != nil {
			err = fmt.Errorf("%w (backup in %s; undo with `cmf restore %s`)", err, backup.Dir(), backup.Stamp)
		}
		return err
	}
	fmt.Fprintf(out, "Done. Backup: %s\n", backup.Dir())
	fmt.Fprintln(out, "Recreate the stack to use it: cd "+h.Dir+" && docker compose up -d --build")
	return nil
}
