package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"

	"github.com/tmargolis/context-memory-fabric/internal/apply"
	"github.com/tmargolis/context-memory-fabric/internal/clients"
	"github.com/tmargolis/context-memory-fabric/internal/features"
	"github.com/tmargolis/context-memory-fabric/internal/home"
)

// Replaced in tests.
var clientEnv = defaultClientEnv

func defaultClientEnv() clients.Env {
	dir, _ := userHome()
	return clients.Env{Home: dir, OS: goos, AppData: os.Getenv("APPDATA"), Getenv: os.Getenv}
}

// bridgePath is this binary, when it lives somewhere an app's config can
// keep pointing at (not `go run`'s build cache or a temp folder). Replaced in tests.
var bridgePath = func() string {
	exe, err := os.Executable()
	if err != nil {
		return ""
	}
	if real, err := filepath.EvalSymlinks(exe); err == nil {
		exe = real
	}
	tmp, _ := filepath.EvalSymlinks(os.TempDir())
	for _, temporary := range []string{tmp, os.TempDir(), "/tmp/", "/private/tmp/", string(filepath.Separator) + "go-build"} {
		if temporary != "" && strings.Contains(exe, temporary) {
			return ""
		}
	}
	return exe
}

// clientTarget is what the entries should reach, from .env.
func clientTarget(env *home.Env) clients.Target {
	t := clients.Target{Port: 8000, PublicURL: env.Get("CMF_MCP_ISSUER_URL"), Token: env.Get("CMF_MCP_AUTH_TOKEN"),
		Bridge: bridgePath(), Home: os.Getenv("CMF_HOME")}
	if p, err := strconv.Atoi(env.Get("CMF_MCP_PORT")); err == nil && p > 0 {
		t.Port = p
	}
	return t
}

func mapValues(m map[string]bool) []bool {
	out := make([]bool, 0, len(m))
	for _, v := range m {
		out = append(out, v)
	}
	return out
}

func stateLine(s clients.State) string {
	cmf := s.CMF()
	switch {
	case s.Err != "":
		return "can't read: " + s.Err
	case !s.Installed:
		return "not installed"
	case len(cmf) == 0:
		return "not set up"
	case s.Healthy():
		if !strings.Contains(cmf[0].URL, "127.0.0.1") && !strings.Contains(cmf[0].URL, "localhost") {
			return "connected (public URL)"
		}
		return "connected"
	case len(cmf) > 1:
		return fmt.Sprintf("%d CMF entries; `cmf clients fix` keeps one", len(cmf))
	}
	return fmt.Sprintf("CMF entry %q %s; `cmf clients fix` replaces it", cmf[0].Name, cmf[0].Problem)
}

func runClients(args []string, out io.Writer) error {
	h, err := home.Default()
	if err != nil {
		return err
	}
	env, err := loadEnv(h)
	if err != nil {
		return err
	}
	target, cenv := clientTarget(env), clientEnv()
	if len(args) == 0 || args[0] == "--json" {
		states := clients.InspectAll(cenv, target)
		if len(args) == 1 {
			enc := json.NewEncoder(out)
			enc.SetIndent("", "  ")
			return enc.Encode(states)
		}
		for _, s := range states {
			fmt.Fprintf(out, "%-15s %s\n", s.Label, stateLine(s))
			if s.Exists {
				fmt.Fprintf(out, "%-15s %s\n", "", s.Path)
			}
		}
		return nil
	}

	verb, rest := args[0], args[1:]
	var ids []string
	var dryRun, yes bool
	for _, a := range rest {
		switch {
		case a == "--dry-run":
			dryRun = true
		case a == "--yes" || a == "-y":
			yes = true
		case strings.HasPrefix(a, "-"):
			return fmt.Errorf("unknown flag %s", a)
		default:
			if _, ok := clients.Find(a); !ok {
				return fmt.Errorf("unknown app %q (one of %s)", a, strings.Join(clients.IDs(), ", "))
			}
			ids = append(ids, a)
		}
	}
	want := map[string]bool{}
	switch verb {
	case "set", "remove":
		if len(ids) == 0 {
			return fmt.Errorf("usage: cmf clients %s <app ...> [--dry-run] [--yes]", verb)
		}
		for _, id := range ids {
			want[id] = verb == "set"
		}
	case "fix":
		if len(ids) > 0 {
			return errors.New("cmf clients fix takes no app names; it repairs every app that has a CMF entry")
		}
		// Only apps that already have CMF entries: fix never adds one.
		for _, s := range clients.InspectAll(cenv, target) {
			if len(s.CMF()) > 0 && !s.Healthy() {
				want[s.ID] = true
			}
		}
		if len(want) == 0 {
			fmt.Fprintln(out, "Nothing to fix: every app with a CMF entry has exactly one, and it reaches this install.")
			return nil
		}
	default:
		return errors.New("usage: cmf clients [--json] | cmf clients set|remove <app ...> | cmf clients fix  [--dry-run] [--yes]")
	}

	if target.PublicURL != "" && target.Token == "" && slices.Contains(mapValues(want), true) {
		// Remote access is on but the apps on this computer have no local
		// token yet: the same plan as Features → Apply creates it, restarts
		// the server to accept it and writes the entries.
		fmt.Fprintln(out, "Remote access is on, so the apps on this computer need a local access token first.")
		return runRequest(&features.Request{Clients: want}, dryRun, yes, out)
	}
	writes, err := planClientWrites(want, target, cenv)
	if err != nil {
		return err
	}
	if len(writes) == 0 {
		fmt.Fprintln(out, "Nothing to change.")
		return nil
	}
	fmt.Fprintln(out, "Review changes:")
	printClientWrites(out, writes)
	if dryRun {
		fmt.Fprintln(out, "Dry run: nothing was changed.")
		return nil
	}
	if !yes && !confirm(out, "Apply?") {
		return errors.New("cancelled; nothing was changed")
	}
	backup, err := executor(h, nil, target.Port, out).Apply(context.Background(), &features.Plan{}, "cmf clients "+verb, writes...)
	if err != nil {
		return err
	}
	afterNotes(out, writes)
	fmt.Fprintf(out, "Done. Undo with: cmf restore %s\n", backup.Stamp)
	return nil
}

// planClientWrites plans each app's change; apps already as wanted drop out.
func planClientWrites(want map[string]bool, target clients.Target, cenv clients.Env) ([]apply.ClientWrite, error) {
	var writes []apply.ClientWrite
	for _, id := range clients.IDs() { // a stable order
		w, ok := want[id]
		if !ok {
			continue
		}
		app, _ := clients.Find(id)
		c, err := clients.Plan(clients.Inspect(app, cenv, target), target, w)
		if err != nil {
			return nil, err
		}
		if !c.Empty() {
			writes = append(writes, apply.ClientWrite{Change: c, Target: target, Env: cenv, Want: w})
		}
	}
	return writes, nil
}

func printClientWrites(out io.Writer, writes []apply.ClientWrite) {
	for _, w := range writes {
		fmt.Fprintf(out, "  %s: write %s (backup kept)\n", w.Change.App.Label, w.Change.Path)
		for _, line := range w.Change.Summary {
			fmt.Fprintln(out, "    -", line)
		}
		if w.Want && w.Change.App.Style == clients.Bridge && w.Target.Bridge == "" && !lookPath("npx") {
			fmt.Fprintf(out, "    - note: %s starts CMF through mcp-remote, which needs Node.js 18 or later (npx wasn't found)\n", w.Change.App.Label)
		}
	}
}

func afterNotes(out io.Writer, writes []apply.ClientWrite) {
	for _, w := range writes {
		fmt.Fprintf(out, "%s: %s\n", w.Change.App.Label, w.Change.App.AfterNote)
	}
}
