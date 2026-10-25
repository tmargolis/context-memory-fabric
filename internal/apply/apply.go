// Package apply carries out a features.Plan or a restore on the machine:
// back up, write ~/.cmf/.env, bring the stack in line, and check that the
// server comes back. If it doesn't, the backup is put back (plan sections
// 3.3 "Review changes" and 3.5 "Undo").
package apply

import (
	"context"
	"encoding/json"
	"fmt"
	"path/filepath"
	"strings"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/clients"
	"github.com/tmargolis/context-memory-fabric/internal/features"
	"github.com/tmargolis/context-memory-fabric/internal/home"
	"github.com/tmargolis/context-memory-fabric/internal/probe"
	"github.com/tmargolis/context-memory-fabric/internal/run"
)

type Executor struct {
	Home     home.Home
	Manifest *features.Manifest
	Runner   run.Runner
	Get      probe.HealthGetter
	Port     int
	Now      func() time.Time
	Sleep    func(time.Duration)
	Log      func(string)
	// HealthTimeout is how long the server gets to answer /health after the
	// stack is recreated (it loads providers and the graph first).
	HealthTimeout time.Duration
}

func (e Executor) compose(args ...string) run.Cmd {
	return run.Cmd{Name: "docker", Args: append([]string{"compose", "--project-directory", e.Home.Dir}, args...), Timeout: 10 * time.Minute}
}

func (e Executor) step(ctx context.Context, note string, cmd run.Cmd) error {
	e.Log("  - " + note)
	if r := e.Runner.Run(ctx, cmd); !r.OK() {
		out := r.Output()
		if len(out) > 400 {
			out = out[len(out)-400:]
		}
		return fmt.Errorf("%s failed (%v): %s", cmd, r.Err, out)
	}
	return nil
}

// waitHealthy polls /health until the server says "ok".
func (e Executor) waitHealthy(ctx context.Context) error {
	url := fmt.Sprintf("http://127.0.0.1:%d/health", e.Port)
	deadline := e.Now().Add(e.HealthTimeout)
	last := ""
	for {
		body, err := e.Get(ctx, url)
		if err == nil {
			var h probe.Health
			if json.Unmarshal(body, &h) == nil && h.Status == "ok" {
				return nil
			}
			last = "status isn't ok: " + strings.TrimSpace(string(body))
		} else {
			last = err.Error()
		}
		if !e.Now().Before(deadline) {
			return fmt.Errorf("the server didn't answer /health within %s (%s)", e.HealthTimeout, last)
		}
		e.Sleep(3 * time.Second)
	}
}

// bringUp stops services a change turned off, then `up -d`, which recreates
// only the containers whose settings changed, then waits for the server.
func (e Executor) bringUp(ctx context.Context, stop []string) error {
	for _, svc := range stop {
		if err := e.step(ctx, "stop "+svc, e.compose("stop", svc)); err != nil {
			return err
		}
	}
	if err := e.step(ctx, "docker compose up -d (recreates the containers whose settings changed)", e.compose("up", "-d")); err != nil {
		return err
	}
	e.Log("  - wait for the CMF server to answer /health")
	return e.waitHealthy(ctx)
}

// Apply writes the plan's .env and brings the stack in line. It returns the
// backup that `cmf restore` takes. On failure it restores that backup and
// says so in the error.
func (e Executor) Apply(ctx context.Context, plan *features.Plan, summary string, writes ...ClientWrite) (*home.Backup, error) {
	backup, err := e.Home.NewBackup(e.Now())
	if err != nil {
		return nil, err
	}
	backup.SaveNote("cmf apply: " + summary)
	// Client configs first: each is backed up into the same set, so a later
	// failure restores them along with .env.
	for _, w := range writes {
		e.Log("  - write " + w.Change.Path + " (backup kept)")
		if err := w.Change.Write(backup, w.Target, w.Env, w.Want); err != nil {
			return backup, e.rollbackFiles(backup, err)
		}
	}
	if plan.Empty() {
		return backup, nil
	}
	e.Log("  - back up " + e.Home.EnvFile() + " to " + backup.Dir())
	if err := backup.Save(e.Home.EnvFile()); err != nil {
		return backup, e.rollbackFiles(backup, fmt.Errorf("backing up .env: %w", err))
	}
	e.Log("  - write " + e.Home.EnvFile())
	if err := plan.Env.Write(e.Home.EnvFile()); err != nil {
		return backup, e.rollbackFiles(backup, fmt.Errorf("writing .env: %w", err))
	}
	if err := e.bringUp(ctx, plan.Stop); err != nil {
		return backup, e.rollback(ctx, backup, err)
	}
	return backup, nil
}

// ClientWrite is one AI app config rewrite that rides along with an Apply.
type ClientWrite struct {
	Change *clients.Change
	Target clients.Target
	Env    clients.Env
	Want   bool
}

// rollbackFiles puts the backed-up files back when the stack wasn't touched yet.
func (e Executor) rollbackFiles(backup *home.Backup, cause error) error {
	e.Log("Failed: " + cause.Error())
	if _, err := e.Home.Restore(backup.Stamp, false, e.Now()); err != nil {
		return fmt.Errorf("%w; restoring %s also failed: %v (restore it by hand from %s)", cause, backup.Stamp, err, backup.Dir())
	}
	return fmt.Errorf("%w; the files written so far were put back", cause)
}

func (e Executor) rollback(ctx context.Context, backup *home.Backup, cause error) error {
	e.Log("Failed: " + cause.Error())
	e.Log("Restoring backup " + backup.Stamp)
	if _, err := e.Home.Restore(backup.Stamp, false, e.Now()); err != nil {
		return fmt.Errorf("%w; restoring %s also failed: %v (restore it by hand from %s)", cause, backup.Stamp, err, backup.Dir())
	}
	if err := e.bringUp(ctx, nil); err != nil {
		return fmt.Errorf("%w; the old settings are back but the stack isn't healthy yet: %v (run `cmf doctor`)", cause, err)
	}
	return fmt.Errorf("%w; the previous settings were restored and the server is back", cause)
}

// Restore puts a backup back (saving the current files first, so the
// restore can be undone) and brings the stack in line.
func (e Executor) Restore(ctx context.Context, stamp string) (*home.Backup, error) {
	before, _ := home.ReadEnv(e.Home.EnvFile())
	info, err := e.Home.Backup(stamp)
	if err != nil {
		return nil, err
	}
	e.Log("  - back up the current files, then put back the ones from " + stamp)
	undo, err := e.Home.Restore(stamp, true, e.Now())
	if err != nil {
		return undo, err
	}
	// A backup of AI app configs alone (cmf clients) leaves the stack as it is.
	stack := false
	for _, f := range info.Files {
		if rel, err := filepath.Rel(e.Home.Dir, f); err == nil && !strings.HasPrefix(rel, "..") {
			stack = true
		}
	}
	if !stack {
		return undo, nil
	}
	after, _ := home.ReadEnv(e.Home.EnvFile())
	return undo, e.bringUp(ctx, e.Manifest.Stops(before, after))
}
