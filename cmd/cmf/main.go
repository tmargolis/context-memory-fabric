// Command cmf is CMF Setup's engine and CLI (docs/setup-simplification-plan.md,
// Phase 3). The GUI (item 3.6) will drive the same packages.
package main

import (
	"fmt"
	"io"
	"os"
	"sort"
)

var version = "dev"

type command struct {
	summary string
	run     func(args []string, out io.Writer) error
}

var commands = map[string]command{
	"mode":    {"show or switch the install mode (standard / developer)", runMode},
	"version": {"print the cmf version", func(_ []string, out io.Writer) error { fmt.Fprintln(out, "cmf", version); return nil }},
}

func usage(out io.Writer) {
	fmt.Fprintln(out, "Usage: cmf <command> [arguments]\n\nCommands:")
	names := make([]string, 0, len(commands))
	for name := range commands {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		fmt.Fprintf(out, "  %-12s %s\n", name, commands[name].summary)
	}
	fmt.Fprintln(out, "\nCMF_HOME picks the install folder (default ~/.cmf).")
}

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

func run(args []string, out, errOut io.Writer) int {
	if len(args) == 0 || args[0] == "help" || args[0] == "-h" || args[0] == "--help" {
		usage(out)
		return 0
	}
	cmd, ok := commands[args[0]]
	if !ok {
		fmt.Fprintf(errOut, "cmf: unknown command %q\n\n", args[0])
		usage(errOut)
		return 2
	}
	if err := cmd.run(args[1:], out); err != nil {
		fmt.Fprintln(errOut, "cmf "+args[0]+":", err)
		return 1
	}
	return 0
}
