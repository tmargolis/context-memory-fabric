package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"os/signal"
	"runtime"
	"strconv"
	"strings"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/checks"
	"github.com/tmargolis/context-memory-fabric/internal/diag"
	"github.com/tmargolis/context-memory-fabric/internal/features"
	"github.com/tmargolis/context-memory-fabric/internal/home"
	"github.com/tmargolis/context-memory-fabric/internal/run"
)

// Replaced in tests.
var (
	userHome = os.UserHomeDir
	lookPath = func(name string) bool { _, err := exec.LookPath(name); return err == nil }
)

func composeLogs(h home.Home, tail int, services ...string) run.Cmd {
	args := []string{"compose", "--project-directory", h.Dir, "logs", "--no-color", "--tail", strconv.Itoa(tail)}
	return run.Cmd{Name: "docker", Args: append(args, services...), Timeout: 30 * time.Second}
}

type logsOpts struct {
	services []string
	tail     int
	follow   bool
	errors   bool
}

func parseLogs(args []string) (logsOpts, error) {
	o := logsOpts{tail: 200}
	usage := errors.New("usage: cmf logs [service ...] [--tail N] [-f|--follow] [--errors]")
	for i := 0; i < len(args); i++ {
		switch a := args[i]; {
		case a == "-f" || a == "--follow":
			o.follow = true
		case a == "--errors":
			o.errors = true
		case a == "--tail" || strings.HasPrefix(a, "--tail="):
			v, ok := strings.CutPrefix(a, "--tail=")
			if !ok {
				if i++; i == len(args) {
					return o, usage
				}
				v = args[i]
			}
			n, err := strconv.Atoi(v)
			if err != nil || n < 1 {
				return o, fmt.Errorf("--tail takes a number of lines, got %q", v)
			}
			o.tail = n
		case strings.HasPrefix(a, "-"):
			return o, usage
		default:
			o.services = append(o.services, a)
		}
	}
	if o.follow && o.errors {
		return o, errors.New("--errors and --follow don't go together")
	}
	return o, nil
}

// runLogs shows the stack's recent logs as they are (they stay on this
// machine); `cmf diagnostics` is the redacted form for sending.
func runLogs(args []string, out io.Writer) error {
	o, err := parseLogs(args)
	if err != nil {
		return err
	}
	h, err := home.Default()
	if err != nil {
		return err
	}
	cmd := composeLogs(h, o.tail, o.services...)
	if o.follow {
		cmd.Args = append(cmd.Args[:4], append([]string{"--follow"}, cmd.Args[4:]...)...)
		cmd.Stream = out
		ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
		defer stop()
		if res := runner.Run(ctx, cmd); res.Err != nil && ctx.Err() == nil {
			return fmt.Errorf("docker compose logs: %v", res.Err)
		}
		return nil
	}
	res := runner.Run(context.Background(), cmd)
	if res.Err != nil {
		return fmt.Errorf("docker compose logs: %s", firstLine(res.Output(), res.Err))
	}
	for _, line := range strings.Split(strings.TrimRight(res.Stdout, "\n"), "\n") {
		if !o.errors || diag.IsError(line) {
			fmt.Fprintln(out, line)
		}
	}
	return nil
}

func firstLine(s string, err error) string {
	if s, _, _ = strings.Cut(s, "\n"); s != "" {
		return s
	}
	return err.Error()
}

type diagOpts struct {
	copy, yes bool
	outFile   string
}

func parseDiagnostics(args []string) (diagOpts, error) {
	var o diagOpts
	for i := 0; i < len(args); i++ {
		switch a := args[i]; {
		case a == "--copy":
			o.copy = true
		case a == "--yes":
			o.yes = true
		case a == "--out" && i+1 < len(args):
			i++
			o.outFile = args[i]
		case strings.HasPrefix(a, "--out="):
			o.outFile = strings.TrimPrefix(a, "--out=")
		default:
			return o, errors.New("usage: cmf diagnostics [--copy [--yes]] [--out FILE]")
		}
	}
	if o.yes && !o.copy {
		return o, errors.New("--yes only goes with --copy")
	}
	return o, nil
}

// runDiagnostics prints the redacted report, and copies or saves it only
// after it has been shown.
func runDiagnostics(args []string, out io.Writer) error {
	o, err := parseDiagnostics(args)
	if err != nil {
		return err
	}
	report, err := buildReport(context.Background())
	if err != nil {
		return err
	}
	fmt.Fprint(out, report)
	if o.outFile != "" {
		if err := home.WriteFile(o.outFile, []byte(report), 0o600); err != nil {
			return err
		}
		fmt.Fprintln(out, "\nSaved to", o.outFile)
	}
	if !o.copy {
		return nil
	}
	if !o.yes && !confirm(out, "\nCopy this report to the clipboard?") {
		fmt.Fprintln(out, "Not copied.")
		return nil
	}
	cmd, ok := clipboard(goos)
	if !ok {
		return errors.New("no clipboard program found (pbcopy, clip, wl-copy or xclip); use --out FILE instead")
	}
	cmd.Stdin = report
	if res := runner.Run(context.Background(), cmd); res.Err != nil {
		return fmt.Errorf("%s: %s", cmd.Name, firstLine(res.Output(), res.Err))
	}
	fmt.Fprintln(out, "Copied.")
	return nil
}

func clipboard(goos string) (run.Cmd, bool) {
	switch goos {
	case "darwin":
		return run.Cmd{Name: "pbcopy"}, true
	case "windows":
		return run.Cmd{Name: "clip"}, true
	}
	for _, c := range []run.Cmd{{Name: "wl-copy"}, {Name: "xclip", Args: []string{"-selection", "clipboard"}}} {
		if lookPath(c.Name) {
			return c, true
		}
	}
	return run.Cmd{}, false
}

func buildReport(ctx context.Context) (string, error) {
	h, err := home.Default()
	if err != nil {
		return "", err
	}
	m, err := features.Load()
	if err != nil {
		return "", err
	}
	env, err := loadEnv(h)
	if err != nil {
		return "", err
	}
	values := map[string]string{}
	for _, k := range env.Keys() {
		values[k] = env.Get(k)
	}
	userDir, _ := userHome()
	hidden := func(k string) bool { return m.SecretKey(k) || diag.Hidden(k) }
	facts, err := collect(ctx)
	if err != nil {
		return "", err
	}
	in := diag.Input{At: now(), Version: version, Arch: runtime.GOARCH, Facts: facts,
		Results: checks.Evaluate(facts), Env: values, Redactor: diag.NewRedactor(values, hidden, userDir)}
	if facts.Docker.Responding {
		res := runner.Run(ctx, composeLogs(h, 5000))
		in.Logs = res.Stdout
		if res.Err != nil {
			in.LogsErr = firstLine(res.Output(), res.Err)
		}
	} else {
		in.LogsErr = "Docker isn't answering"
	}
	return diag.Build(in), nil
}
