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
	"mode":        {"show or switch the install mode (standard / developer)", runMode},
	"status":      {"check this CMF install (one line per check)", runStatus},
	"doctor":      {"the checks with their fixes; --fix runs them, --json for scripts", runDoctor},
	"features":    {"list the features and their current settings (--json)", runFeatures},
	"apply":       {"apply a feature file: review, back up, write, recreate, verify", runApply},
	"restore":     {"list backups, or put one back (cmf restore <stamp>)", runRestore},
	"logs":        {"recent logs of the stack (service names, --tail N, -f, --errors)", runLogs},
	"diagnostics": {"a redacted report to send for help; shown first, then --copy or --out", runDiagnostics},
	"selftest":    {"write, recall and delete a test memory in a scratch graph (calls your providers)", runSelftest},
	"clients":     {"each AI app's CMF entry: list, set / remove <app>, fix duplicates", runClients},
	"migrate":     {"move a repo-based install (host or preview stack) into ~/.cmf: plan, apply, restore <stamp>", runMigrate},
	"ui":          {"open CMF Setup in the browser (local only; --no-open prints the address)", runUI},
	"bridge":      {"relay an AI app's stdio MCP to the CMF server (what Claude Desktop's entry runs)", runBridge},
	"remote":      {"remote access for connectors: status, enable tailscale, use <url>, disable", runRemote},
	"version":     {"print the cmf version", func(_ []string, out io.Writer) error { fmt.Fprintln(out, "cmf", version); return nil }},
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
	fmt.Fprintln(out, "\nWith no command, cmf opens CMF Setup in your browser (same as `cmf ui`).")
	fmt.Fprintln(out, "CMF_HOME picks the install folder (default ~/.cmf).")
}

func main() {
	os.Exit(runCLI(os.Args[1:], os.Stdout, os.Stderr))
}

func runCLI(args []string, out, errOut io.Writer) int {
	if len(args) == 0 {
		// Double-clicking the binary opens the app (plan section 3.1).
		args = []string{"ui"}
	}
	if args[0] == "help" || args[0] == "-h" || args[0] == "--help" {
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
