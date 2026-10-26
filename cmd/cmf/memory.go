package main

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"os/signal"

	"github.com/tmargolis/context-memory-fabric/internal/checks"
	"github.com/tmargolis/context-memory-fabric/internal/home"
)

// runMemory: `cmf memory retry` re-saves memories whose background write
// failed, from the copy the server keeps until a save commits (B20).
func runMemory(args []string, out io.Writer) error {
	if len(args) != 1 || args[0] != "retry" {
		return errors.New("usage: cmf memory retry")
	}
	h, err := home.Default()
	if err != nil {
		return err
	}
	cmd := checks.MemoryRetryCmd(h.Dir)
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	ctx, cancel := context.WithTimeout(ctx, cmd.Timeout)
	defer cancel()
	cmd.Stream = out
	res := runner.Run(ctx, cmd)
	if res.Err != nil {
		return fmt.Errorf("some saves failed again (reasons above), or the server couldn't run the retry: %v", res.Err)
	}
	return nil
}
