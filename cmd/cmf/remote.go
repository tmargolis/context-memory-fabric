package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os/exec"
	"strings"

	"github.com/tmargolis/context-memory-fabric/internal/features"
	"github.com/tmargolis/context-memory-fabric/internal/home"
	"github.com/tmargolis/context-memory-fabric/internal/remote"
)

// Replaced in tests.
var findProgram remote.Finder = func(name string) (string, bool) {
	if strings.Contains(name, "/") {
		if home.Exists(name) {
			return name, true
		}
		return "", false
	}
	p, err := exec.LookPath(name)
	return p, err == nil
}

// runRemote is the Remote access helper (item 3.10).
func runRemote(args []string, out io.Writer) error {
	h, err := home.Default()
	if err != nil {
		return err
	}
	env, err := loadEnv(h)
	if err != nil {
		return err
	}
	ctx := context.Background()
	port := clientTarget(env).Port
	verb := ""
	if len(args) > 0 {
		verb, args = args[0], args[1:]
	}
	var dryRun, yes bool
	var rest []string
	for _, a := range args {
		switch a {
		case "--dry-run":
			dryRun = true
		case "--yes", "-y":
			yes = true
		default:
			rest = append(rest, a)
		}
	}
	usage := errors.New("usage: cmf remote | cmf remote enable tailscale | cmf remote use https://<host> | cmf remote disable  [--dry-run] [--yes]")

	switch verb {
	case "", "--json":
		tools := remote.Detect(ctx, runner, findProgram, port)
		if verb == "--json" {
			enc := json.NewEncoder(out)
			enc.SetIndent("", "  ")
			return enc.Encode(map[string]any{"issuer_url": env.Get("CMF_MCP_ISSUER_URL"), "tools": tools})
		}
		issuer := env.Get("CMF_MCP_ISSUER_URL")
		if issuer != "" {
			fmt.Fprintf(out, "Remote access: on. Connectors use %s/mcp (OAuth; the consent page asks for CMF_MCP_OAUTH_PASSWORD from ~/.cmf/.env).\n", issuer)
		} else {
			fmt.Fprintln(out, "Remote access: off. Connectors in ChatGPT, Gemini or claude.ai need a public HTTPS address and OAuth.")
		}
		for _, t := range tools {
			fmt.Fprintf(out, "  %-12s %s\n", t.Kind, t.Detail)
			if t.Kind == remote.Tailscale && t.ExposedURL != "" && issuer != "" && t.ExposedURL != issuer {
				fmt.Fprintf(out, "  %-12s note: CMF_MCP_ISSUER_URL is %s, but Funnel publishes CMF at %s; OAuth needs them to match\n", "", issuer, t.ExposedURL)
			}
		}
		if rec, err := remote.LoadRecord(h); err == nil {
			fmt.Fprintf(out, "Set up by cmf remote (%s, %s); `cmf remote disable` undoes it.\n", rec.Kind, rec.PublicURL)
		}
		return nil

	case "enable", "use":
		var plan *remote.Plan
		if verb == "enable" {
			if len(rest) != 1 || rest[0] != remote.Tailscale {
				return errors.New("cmf remote enable tailscale (for cloudflared, ngrok or another tunnel you run: cmf remote use https://<its address>)")
			}
			var ts remote.Tool
			for _, t := range remote.Detect(ctx, runner, findProgram, port) {
				if t.Kind == remote.Tailscale {
					ts = t
				}
			}
			plan, err = remote.PlanTailscale(ts, env, port)
		} else {
			if len(rest) != 1 {
				return usage
			}
			plan, err = remote.PlanURL(rest[0], env)
		}
		if err != nil {
			return err
		}
		return applyRemote(ctx, h, env, plan, port, false, dryRun, yes, out)

	case "disable":
		rec, err := remote.LoadRecord(h)
		if err != nil {
			return errors.New("nothing to undo: remote access wasn't set up by `cmf remote` (state/remote.json is missing)")
		}
		return applyRemote(ctx, h, env, remote.UndoPlan(rec, env), port, true, dryRun, yes, out)
	}
	return usage
}

func applyRemote(ctx context.Context, h home.Home, env *home.Env, plan *remote.Plan, port int, undo, dryRun, yes bool, out io.Writer) error {
	m, err := features.Load()
	if err != nil {
		return err
	}
	fp := &features.Plan{Env: plan.Env}
	for _, k := range []string{"CMF_MCP_ISSUER_URL", "CMF_MCP_OAUTH_PASSWORD"} {
		if env.Get(k) != plan.Env.Get(k) {
			fp.Changes = append(fp.Changes, features.Change{Key: k, Old: env.Get(k), New: plan.Env.Get(k), Removed: !plan.Env.Has(k), Secret: k == "CMF_MCP_OAUTH_PASSWORD"})
		}
	}
	fmt.Fprintln(out, "Review changes:")
	for _, l := range plan.Lines {
		fmt.Fprintln(out, "  - "+l)
	}
	if !fp.Empty() {
		fmt.Fprintln(out, "  - Recreate the CMF server with the new settings, then wait for it")
	}
	if len(plan.Lines) == 0 {
		fmt.Fprintln(out, "Nothing to change.")
		return nil
	}
	if dryRun {
		fmt.Fprintln(out, "Dry run: nothing was changed.")
		return nil
	}
	facts, err := collect(ctx)
	if err != nil {
		return err
	}
	if !fp.Empty() && !facts.Docker.Responding {
		return errors.New("Docker isn't answering, so the server can't be recreated; run `cmf doctor` first")
	}
	if !yes && !confirm(out, "Apply?") {
		return errors.New("cancelled; nothing was changed")
	}
	for _, cmd := range plan.Commands { // at most one: CMF's own Funnel port
		fmt.Fprintln(out, "  - "+cmd.String())
		if res := runner.Run(ctx, cmd); res.Err != nil {
			return fmt.Errorf("`%s` failed: %s", cmd, strings.TrimSpace(res.Output()+" "+res.Err.Error()))
		}
	}
	var backup *home.Backup
	if !fp.Empty() {
		backup, err = executor(h, m, port, out).Apply(ctx, fp, "cmf remote")
		if err != nil {
			if !undo && plan.Record.HTTPSPort != 0 {
				off := remote.UndoPlan(&remote.Record{Kind: remote.Tailscale, HTTPSPort: plan.Record.HTTPSPort, TailscalePath: plan.Record.TailscalePath, PublicURL: plan.Record.PublicURL}, env)
				for _, cmd := range off.Commands {
					runner.Run(ctx, cmd)
				}
			}
			return err
		}
	}
	if undo {
		if err := remote.RemoveRecord(h); err != nil {
			return err
		}
		fmt.Fprintln(out, "Remote access is off.")
		return nil
	}
	if err := remote.SaveRecord(h, &plan.Record); err != nil {
		return err
	}
	public := plan.Record.PublicURL + "/health"
	if body, err := getHealth(ctx, public); err != nil || !strings.Contains(string(body), `"status"`) {
		fmt.Fprintf(out, "Note: %s didn't answer yet (%v). A new Funnel address can take a minute; check with `curl %s`.\n", public, err, public)
	} else {
		fmt.Fprintln(out, "Checked: "+public+" answers.")
	}
	fmt.Fprintf(out, "Done. Add a connector with the address %s/mcp; its consent page asks for CMF_MCP_OAUTH_PASSWORD (in ~/.cmf/.env).\n", plan.Record.PublicURL)
	if backup != nil {
		fmt.Fprintf(out, "Undo with: cmf remote disable  (or cmf restore %s for the settings alone)\n", backup.Stamp)
	}
	return nil
}
