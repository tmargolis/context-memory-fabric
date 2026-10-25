package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/tmargolis/context-memory-fabric/internal/home"
	"github.com/tmargolis/context-memory-fabric/internal/migrate"
)

func migratePaths(repo string) (migrate.Paths, error) {
	h, err := home.Default()
	if err != nil {
		return migrate.Paths{}, err
	}
	user, err := userHome()
	if err != nil {
		return migrate.Paths{}, err
	}
	p := migrate.Paths{Repo: repo, UserHome: user, Home: h}
	if goos == "darwin" {
		p.LaunchAgents = filepath.Join(user, "Library", "LaunchAgents")
	}
	return p, nil
}

func migrateOps(out io.Writer) migrate.Ops {
	return migrate.Ops{Runner: runner, Get: getHealth, Sleep: sleep, Now: now,
		Log: func(s string) { fmt.Fprintln(out, s) }, UID: func() string { return strconv.Itoa(os.Getuid()) }}
}

// runMigrate moves a repo-based install (host or preview stack) into ~/.cmf (item 3.5).
func runMigrate(args []string, out io.Writer) error {
	verb := "plan"
	if len(args) > 0 && !strings.HasPrefix(args[0], "-") {
		verb, args = args[0], args[1:]
	}
	var repo, stamp string
	var yes bool
	for i := 0; i < len(args); i++ {
		switch a := args[i]; {
		case a == "--yes" || a == "-y":
			yes = true
		case a == "--repo" && i+1 < len(args):
			i++
			repo = args[i]
		case strings.HasPrefix(a, "--repo="):
			repo = strings.TrimPrefix(a, "--repo=")
		case verb == "restore" && stamp == "" && !strings.HasPrefix(a, "-"):
			stamp = a
		default:
			return errors.New("usage: cmf migrate [plan|apply] [--repo PATH] [--yes] | cmf migrate restore <stamp> [--yes]")
		}
	}
	if repo != "" && !filepath.IsAbs(repo) {
		return fmt.Errorf("--repo %s: use an absolute path", repo)
	}
	p, err := migratePaths(repo)
	if err != nil {
		return err
	}
	ctx, o := context.Background(), migrateOps(out)

	switch verb {
	case "restore":
		if stamp == "" {
			return errors.New("cmf migrate restore needs the stamp `cmf migrate apply` printed")
		}
		if !yes && !confirm(out, "Undo migration "+stamp+": stop the new stack, put the repo .env back, re-enable the launchd jobs and start the old containers?") {
			return errors.New("cancelled; nothing was changed")
		}
		return migrate.Restore(ctx, p, o, stamp)
	case "plan", "apply":
	default:
		return fmt.Errorf("unknown migrate step %q (plan, apply or restore)", verb)
	}
	s := migrate.Look(ctx, p, o)
	if !s.Migratable(p) {
		fmt.Fprintln(out, "Nothing to migrate:")
		for _, b := range s.Blockers {
			fmt.Fprintln(out, "  -", b)
		}
		return nil
	}
	stamp = now().Format(home.StampFormat)
	for _, line := range migrate.Describe(p, s, stamp) {
		fmt.Fprintln(out, line)
	}
	if len(s.Blockers) > 0 {
		fmt.Fprintln(out, "\nBlocked:")
		for _, b := range s.Blockers {
			fmt.Fprintln(out, "  -", b)
		}
		if verb == "apply" {
			return errors.New("nothing was changed")
		}
		return nil
	}
	if verb == "plan" {
		fmt.Fprintln(out, "\nReady. Run `cmf migrate apply` to do it.")
		return nil
	}
	if !yes && !confirm(out, "\nMigrate now?") {
		return errors.New("cancelled; nothing was changed")
	}
	_, err = migrate.Apply(ctx, p, o, s, stamp)
	return err
}
