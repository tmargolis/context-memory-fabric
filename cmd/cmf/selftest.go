package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"
	"strings"

	"github.com/tmargolis/context-memory-fabric/internal/checks"
	"github.com/tmargolis/context-memory-fabric/internal/home"
)

// runSelftest runs the server's self-test (item 1.7) in the cmf-server
// container and shows its stages as they finish.
func runSelftest(args []string, out io.Writer) error {
	if len(args) > 0 {
		return errors.New("cmf selftest takes no arguments")
	}
	h, err := home.Default()
	if err != nil {
		return err
	}
	cmd := checks.SelftestCmd(h.Dir)
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	ctx, cancel := context.WithTimeout(ctx, cmd.Timeout)
	defer cancel()
	var seen strings.Builder
	cmd.Stream = io.MultiWriter(out, &seen)
	fmt.Fprintln(out, "Writing a test memory to a scratch graph, recalling it, and deleting the graph (a few minutes)...")
	res := runner.Run(ctx, cmd)
	switch {
	case res.Err == nil:
		return nil
	case strings.Contains(seen.String(), "No module named server.selftest"):
		return errors.New("this server predates the self-test; rebuild it (`docker compose up -d --build` in ~/.cmf)")
	case strings.Contains(seen.String(), "CMF self-test FAILED"):
		return errors.New("the self-test failed (stages above)")
	case ctx.Err() == context.DeadlineExceeded:
		return fmt.Errorf("the self-test didn't finish within %s", cmd.Timeout)
	}
	return fmt.Errorf("couldn't run the self-test in cmf-server: %v", res.Err)
}
