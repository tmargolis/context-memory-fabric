// Package run is the one way cmf runs other programs (docker, launchctl,
// lsof, ...). Checks and fixes take a Runner, so tests use Fake and never
// touch the machine.
package run

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"strings"
	"time"
)

// DefaultTimeout bounds every command: a hung Docker daemon must turn into
// a red row, not a hung cmf.
const DefaultTimeout = 15 * time.Second

var (
	ErrNotFound = errors.New("program not found")
	ErrTimeout  = errors.New("no answer before the timeout")
)

type Cmd struct {
	Name    string
	Args    []string
	Env     []string // extra KEY=value pairs, on top of this process's environment
	Dir     string
	Timeout time.Duration
}

func (c Cmd) String() string {
	return strings.TrimSpace(c.Name + " " + strings.Join(c.Args, " "))
}

type Result struct {
	Stdout string
	Stderr string
	Err    error // nil, ErrNotFound, ErrTimeout, or the exit error
}

func (r Result) OK() bool { return r.Err == nil }

// Output is stdout, or stderr when stdout is empty, trimmed.
func (r Result) Output() string {
	if s := strings.TrimSpace(r.Stdout); s != "" {
		return s
	}
	return strings.TrimSpace(r.Stderr)
}

type Runner interface {
	Run(ctx context.Context, cmd Cmd) Result
}

type Exec struct{}

func (Exec) Run(ctx context.Context, c Cmd) Result {
	timeout := c.Timeout
	if timeout == 0 {
		timeout = DefaultTimeout
	}
	ctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()
	path, err := exec.LookPath(c.Name)
	if err != nil {
		return Result{Err: fmt.Errorf("%w: %s", ErrNotFound, c.Name)}
	}
	cmd := exec.CommandContext(ctx, path, c.Args...)
	cmd.Dir = c.Dir
	if len(c.Env) > 0 {
		cmd.Env = append(os.Environ(), c.Env...)
	}
	var stdout, stderr bytes.Buffer
	cmd.Stdout, cmd.Stderr = &stdout, &stderr
	// Don't wait on grandchildren that keep the pipes open after a kill.
	cmd.WaitDelay = 2 * time.Second
	err = cmd.Run()
	if ctx.Err() == context.DeadlineExceeded {
		err = ErrTimeout
	}
	return Result{Stdout: stdout.String(), Stderr: stderr.String(), Err: err}
}

// Fake answers commands from a table keyed by Cmd.String() (or a prefix of
// it ending in "*"), and records every call.
type Fake struct {
	Answers map[string]Result
	Calls   []Cmd
}

func (f *Fake) Run(_ context.Context, c Cmd) Result {
	f.Calls = append(f.Calls, c)
	key := c.String()
	if r, ok := f.Answers[key]; ok {
		return r
	}
	best := ""
	for pattern := range f.Answers {
		if prefix, ok := strings.CutSuffix(pattern, "*"); ok && strings.HasPrefix(key, prefix) && len(prefix) > len(best) {
			best = prefix
		}
	}
	if best != "" {
		return f.Answers[best+"*"]
	}
	return Result{Err: fmt.Errorf("%w: fake has no answer for %q", ErrNotFound, key)}
}

// Ran reports whether a call matching key (exact, or prefix with "*") was made.
func (f *Fake) Ran(key string) bool {
	prefix, isPrefix := strings.CutSuffix(key, "*")
	for _, c := range f.Calls {
		if s := c.String(); s == key || (isPrefix && strings.HasPrefix(s, prefix)) {
			return true
		}
	}
	return false
}
