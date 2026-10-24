package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"runtime"
	"strings"

	"github.com/tmargolis/context-memory-fabric/internal/checks"
	"github.com/tmargolis/context-memory-fabric/internal/home"
	"github.com/tmargolis/context-memory-fabric/internal/probe"
	"github.com/tmargolis/context-memory-fabric/internal/run"
)

// Replaced in tests.
var (
	runner    run.Runner         = run.Exec{}
	getHealth probe.HealthGetter = probe.HTTPGet
	goos                         = runtime.GOOS
)

var errChecksFailed = errors.New("some checks failed")

func collect(ctx context.Context) (probe.Facts, error) {
	h, err := home.Default()
	if err != nil {
		return probe.Facts{}, err
	}
	p := probe.Prober{Home: h, Runner: runner, Get: getHealth, OS: goos, Now: now}
	return p.Collect(ctx), nil
}

var marks = map[checks.Level]string{checks.OK: "ok  ", checks.Warn: "WARN", checks.Fail: "FAIL", checks.NA: "n/a "}

func printResults(out io.Writer, results []checks.Result, details bool) {
	for _, r := range results {
		fmt.Fprintf(out, "[%s] %s: %s\n", marks[r.Level], r.Title, r.Detail)
		if details && r.Fix != nil && r.Level != checks.OK {
			fmt.Fprintf(out, "       Fix (cmf doctor --fix=%s): %s\n", r.ID, r.Fix.Label)
			for _, s := range r.Fix.Steps {
				fmt.Fprintf(out, "         $ %s\n", s.Show)
			}
		}
	}
}

func runStatus(args []string, out io.Writer) error {
	if len(args) > 0 {
		return fmt.Errorf("cmf status takes no arguments (see cmf doctor)")
	}
	facts, err := collect(context.Background())
	if err != nil {
		return err
	}
	results := checks.Evaluate(facts)
	printResults(out, results, false)
	if checks.Worst(results) == checks.Fail {
		fmt.Fprintln(out, "\nRun `cmf doctor` for the fixes.")
		return errChecksFailed
	}
	return nil
}

type doctorOpts struct {
	json bool
	fix  bool
	only string
}

func parseDoctor(args []string) (doctorOpts, error) {
	var o doctorOpts
	for _, a := range args {
		switch {
		case a == "--json":
			o.json = true
		case a == "--fix":
			o.fix = true
		case strings.HasPrefix(a, "--fix="):
			o.fix, o.only = true, strings.TrimPrefix(a, "--fix=")
		default:
			return o, fmt.Errorf("unknown argument %q (usage: cmf doctor [--json] [--fix | --fix=<check>])", a)
		}
	}
	if o.json && o.fix {
		return o, errors.New("--json and --fix can't be combined")
	}
	return o, nil
}

func runDoctor(args []string, out io.Writer) error {
	opts, err := parseDoctor(args)
	if err != nil {
		return err
	}
	ctx := context.Background()
	facts, err := collect(ctx)
	if err != nil {
		return err
	}
	results := checks.Evaluate(facts)

	if opts.json {
		enc := json.NewEncoder(out)
		enc.SetIndent("", "  ")
		return enc.Encode(map[string]any{"overall": checks.Worst(results), "checked_at": facts.At, "checks": results})
	}
	printResults(out, results, true)
	if !opts.fix {
		if checks.Worst(results) == checks.Fail {
			return errChecksFailed
		}
		return nil
	}

	var todo []checks.Result
	for _, r := range results {
		if r.Fix != nil && r.Level != checks.OK && (opts.only == "" || r.ID == opts.only) {
			todo = append(todo, r)
		}
	}
	if opts.only != "" && len(todo) == 0 {
		return fmt.Errorf("nothing to fix for %q", opts.only)
	}
	if len(todo) == 0 {
		fmt.Fprintln(out, "\nNothing cmf can fix.")
		return nil
	}
	fmt.Fprintln(out)
	var fixErrs []string
	for _, r := range todo {
		fmt.Fprintf(out, "Fixing %s: %s\n", r.ID, r.Fix.Label)
		if err := checks.ApplyFix(ctx, runner, r.Fix, func(s string) { fmt.Fprintln(out, s) }); err != nil {
			fmt.Fprintln(out, "  failed:", err)
			fixErrs = append(fixErrs, r.ID)
			// Later fixes (e.g. starting the stack) depend on this one.
			if r.ID == "docker" {
				break
			}
		}
	}
	fmt.Fprintln(out, "\nChecking again:")
	facts, _ = collect(ctx)
	results = checks.Evaluate(facts)
	printResults(out, results, false)
	if len(fixErrs) > 0 {
		return fmt.Errorf("fixes failed: %s", strings.Join(fixErrs, ", "))
	}
	if checks.Worst(results) == checks.Fail {
		return errChecksFailed
	}
	return nil
}
