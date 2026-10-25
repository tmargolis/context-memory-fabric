package features

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"slices"
	"sort"
	"strings"

	"github.com/tmargolis/context-memory-fabric/internal/capture"
	"github.com/tmargolis/context-memory-fabric/internal/home"
)

// Request is a feature file: only the features to change. Each entry may
// hold "enabled" and any of the feature's input ids; features not listed
// stay as they are on the machine.
//
//	{"features": {"notion": {"enabled": true, "token": "secret_…"},
//	              "llm": {"providers": ["anthropic", "local"]}}}
type Request struct {
	Features map[string]map[string]json.RawMessage `json:"features"`
	// Clients turns each AI app's CMF entry on or off (item 3.4), by app id
	// (`cmf clients` lists them). Written by the cmd layer, not the manifest.
	Clients map[string]bool `json:"clients,omitempty"`
}

func ParseRequest(data []byte) (*Request, error) {
	var r Request
	dec := json.NewDecoder(strings.NewReader(string(data)))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&r); err != nil {
		return nil, fmt.Errorf("feature file: %w", err)
	}
	if len(r.Features) == 0 && len(r.Clients) == 0 {
		return nil, fmt.Errorf(`feature file: nothing to change (expected {"features": {...}} and/or {"clients": {...}})`)
	}
	return &r, nil
}

// Context is what the rules need besides .env.
type Context struct {
	Repo  string // developer mode's checkout, "" in standard mode
	Home  string // the CMF home (~/.cmf)
	Nodes int    // graph node count, -1 when unknown
	User  string // the user's home folder, for capture's transcript folders; "" skips them
}

type Change struct {
	Key     string `json:"key"`
	Old     string `json:"old"`
	New     string `json:"new"`
	Removed bool   `json:"removed,omitempty"`
	Secret  bool   `json:"secret,omitempty"`
}

func (c Change) String() string {
	old, new := c.Old, c.New
	if c.Secret {
		old, new = Mask(old), Mask(new)
	}
	switch {
	case c.Removed:
		return fmt.Sprintf("remove %s (was %s)", c.Key, show(old))
	case c.Old == "":
		return fmt.Sprintf("set %s=%s", c.Key, new)
	}
	return fmt.Sprintf("set %s=%s (was %s)", c.Key, new, show(old))
}

func show(v string) string {
	if v == "" {
		return "empty"
	}
	return v
}

type Plan struct {
	Env      *home.Env `json:"-"` // .env as it will be written
	Changes  []Change  `json:"changes"`
	Enabled  []string  `json:"enabled"`  // features turned on
	Disabled []string  `json:"disabled"` // features turned off
	Stop     []string  `json:"stop"`     // compose services to stop
	Notes    []string  `json:"notes"`    // cost notes and other things to know
	Warnings []string  `json:"warnings"` // rule problems that were already there
	Errors   []string  `json:"errors"`
	// StarterWiki is where Apply writes the demo wiki before the stack
	// starts ("" = nothing to write; Developer mode uses the checkout's).
	StarterWiki string `json:"starter_wiki,omitempty"`
}

func (p *Plan) OK() bool                { return len(p.Errors) == 0 }
func (p *Plan) Empty() bool             { return len(p.Changes) == 0 && len(p.Stop) == 0 }
func (p *Plan) fail(f string, a ...any) { p.Errors = append(p.Errors, fmt.Sprintf(f, a...)) }

// SecretKey reports .env keys whose values never appear in a plan, log or
// diagnostics report.
func (m *Manifest) SecretKey(key string) bool {
	for _, f := range m.Features {
		for _, in := range f.Inputs {
			if in.Env == key && in.Type == TypeSecret {
				return true
			}
		}
	}
	upper := strings.ToUpper(key)
	for _, word := range []string{"KEY", "TOKEN", "PASSWORD", "SECRET"} {
		if strings.Contains(upper, word) {
			return true
		}
	}
	return false
}

// Plan works out what the request changes. It writes nothing. Problems are
// collected so the user sees them all at once.
func (m *Manifest) Plan(current *home.Env, req *Request, ctx Context) *Plan {
	if current == nil {
		current = &home.Env{}
	}
	p := &Plan{Env: current.Clone()}

	// Manifest order, so the plan reads the same every time.
	var ids []string
	for id := range req.Features {
		if m.Feature(id) == nil {
			p.fail("unknown feature %q (see `cmf features`)", id)
			continue
		}
		ids = append(ids, id)
	}
	sort.Slice(ids, func(i, j int) bool { return m.index(ids[i]) < m.index(ids[j]) })

	for _, id := range ids {
		m.planFeature(p, current, m.Feature(id), req.Features[id], ctx)
	}
	// Keep going after request errors: the rules below see the parts of the
	// request that were valid, so every problem is reported in one pass.

	for _, f := range m.Features {
		if f.Derived || f.AlwaysOn {
			continue
		}
		if !m.Enabled(current, f.ID) && m.Enabled(p.Env, f.ID) {
			p.Enabled = append(p.Enabled, f.ID)
			if f.CostNote != "" {
				p.Notes = append(p.Notes, f.Label+": "+f.CostNote)
			}
		}
	}
	for _, f := range m.Features {
		if !m.Enabled(p.Env, f.ID) {
			continue
		}
		for _, in := range f.Inputs {
			if in.Required && Value(p.Env, &in) == "" {
				p.fail("%s needs %s (%q in the feature file)", f.Label, in.Label, in.ID)
			}
		}
	}

	before := map[string]bool{}
	for _, msg := range m.violations(current, current, ctx) {
		before[msg] = true
	}
	for _, msg := range m.violations(current, p.Env, ctx) {
		if before[msg] {
			p.Warnings = append(p.Warnings, msg+" (already the case before this change)")
		} else {
			p.Errors = append(p.Errors, msg)
		}
	}
	// Capture is always on: the poller runs (the compose profile) and mounts
	// every AI app's transcript folder on this computer; unset, compose
	// mounts empty ones and nothing is captured.
	if ctx.User != "" {
		m.write(p, m.Feature("capture"), &Writes{ListAdd: map[string]string{"COMPOSE_PROFILES": "capture"}}, ctx)
		for key, dir := range capture.DefaultFolders(ctx.User, p.Env) {
			p.Env.Set(key, dir)
		}
	}
	// With remote access on (OAuth), the server lets the AI apps on this
	// computer in only with CMF_MCP_AUTH_TOKEN, which their entries carry.
	if p.Env.Get("CMF_MCP_ISSUER_URL") != "" && p.Env.Get("CMF_MCP_AUTH_TOKEN") == "" && slices.Contains(mapValues(req.Clients), true) {
		p.Env.Set("CMF_MCP_AUTH_TOKEN", NewToken())
		p.Notes = append(p.Notes, "Remote access is on, so the AI apps on this computer get a local access token (CMF_MCP_AUTH_TOKEN); the server restarts to accept it.")
	}
	p.Changes = m.diff(current, p.Env)
	return p
}

// NewToken is a random 256-bit token, hex.
func NewToken() string {
	b := make([]byte, 32)
	rand.Read(b)
	return hex.EncodeToString(b)
}

func mapValues(m map[string]bool) []bool {
	out := make([]bool, 0, len(m))
	for _, v := range m {
		out = append(out, v)
	}
	return out
}

func (m *Manifest) index(id string) int {
	for i, f := range m.Features {
		if f.ID == id {
			return i
		}
	}
	return len(m.Features)
}

func (m *Manifest) planFeature(p *Plan, current *home.Env, f *Feature, fields map[string]json.RawMessage, ctx Context) {
	if f.Derived {
		p.fail("%s can't be set directly: %s", f.ID, f.Description)
		return
	}
	if f.ComingSoon {
		p.fail("%s is coming soon; it can't be turned on yet", f.Label)
		return
	}
	was := m.Enabled(current, f.ID)
	want := was
	if raw, ok := fields["enabled"]; ok {
		if err := json.Unmarshal(raw, &want); err != nil {
			p.fail("%s.enabled must be true or false", f.ID)
			return
		}
		if f.AlwaysOn && !want {
			p.fail("%s is always on", f.Label)
			return
		}
	}
	hasInputs := len(fields) > 0 && !(len(fields) == 1 && fields["enabled"] != nil)
	if !want && hasInputs {
		p.fail("%s is off; add \"enabled\": true to set its inputs", f.Label)
		return
	}
	switch {
	case want && !was && f.OnEnable != nil:
		m.write(p, f, f.OnEnable, ctx)
	case !want && was && f.OnDisable != nil:
		m.write(p, f, f.OnDisable, ctx)
	}
	var names []string
	for name := range fields {
		names = append(names, name)
	}
	sort.Strings(names)
	for _, name := range names {
		if name == "enabled" {
			continue
		}
		in := f.Input(name)
		if in == nil {
			p.fail("%s has no input %q", f.ID, name)
			continue
		}
		values, err := m.parseInput(f, in, fields[name])
		if err != nil {
			p.fail("%s.%s: %v", f.ID, name, err)
			continue
		}
		for _, key := range in.Replaces {
			if was := p.Env.Get(key); was != "" {
				p.Env.Unset(key)
				p.Notes = append(p.Notes, strings.ReplaceAll(in.ReplacesNote, "{{was}}", was))
			}
		}
		setInput(p.Env, in, values)
	}
}

func (m *Manifest) forbidden(f *Feature, in *Input, value string) bool {
	for _, r := range m.Rules {
		if r.Type == RuleForbidValue && r.Feature == f.ID && r.Input == in.ID && slices.Contains(r.Values, value) {
			return true
		}
	}
	return false
}

// parseInput checks a feature-file value against the input's type. Values
// a forbid_value rule covers are passed through for the rule's message.
func (m *Manifest) parseInput(f *Feature, in *Input, raw json.RawMessage) ([]string, error) {
	var values []string
	var one string
	if err := json.Unmarshal(raw, &one); err == nil {
		if in.Type == TypeRanked {
			values = List(one)
		} else if one != "" {
			values = []string{strings.TrimSpace(one)}
		}
	} else if err := json.Unmarshal(raw, &values); err != nil || in.Type != TypeRanked {
		return nil, fmt.Errorf("expected a string%s", map[bool]string{true: " or a list"}[in.Type == TypeRanked])
	}
	switch in.Type {
	case TypeChoice, TypeRanked:
		seen := map[string]bool{}
		for i, v := range values {
			v = strings.ToLower(strings.TrimSpace(v))
			values[i] = v
			if seen[v] {
				return nil, fmt.Errorf("%q is listed twice", v)
			}
			seen[v] = true
			if !slices.Contains(in.Choices, v) && !m.forbidden(f, in, v) {
				return nil, fmt.Errorf("%q isn't one of %s", v, strings.Join(in.Choices, ", "))
			}
		}
		if in.Type == TypeRanked && len(values) == 0 && in.Required {
			return nil, fmt.Errorf("list at least one of %s", strings.Join(in.Choices, ", "))
		}
	case TypeURL:
		if len(values) == 1 && !strings.HasPrefix(values[0], "http://") && !strings.HasPrefix(values[0], "https://") {
			return nil, fmt.Errorf("%q isn't an http(s) URL", values[0])
		}
	case TypePath:
		if len(values) == 1 {
			path := values[0]
			if strings.HasPrefix(path, "~/") {
				if user, err := os.UserHomeDir(); err == nil {
					path = filepath.Join(user, path[2:])
				}
			}
			if !filepath.IsAbs(path) {
				return nil, fmt.Errorf("%q: use an absolute path", values[0])
			}
			if info, err := os.Stat(path); err != nil || !info.IsDir() {
				return nil, fmt.Errorf("%s isn't a folder on this computer", path)
			}
			values[0] = path
		}
	}
	return values, nil
}

func setInput(env *home.Env, in *Input, values []string) {
	if len(values) == 0 {
		env.Unset(in.Env)
		if in.FirstEnv != "" {
			env.Unset(in.FirstEnv)
		}
		return
	}
	env.Set(in.Env, strings.Join(values, ","))
	if in.FirstEnv != "" {
		env.Set(in.FirstEnv, values[0])
	}
}

func (m *Manifest) write(p *Plan, f *Feature, w *Writes, ctx Context) {
	for _, key := range sortedKeys(w.Set) {
		value := w.Set[key]
		if strings.Contains(value, "{{starter_wiki}}") {
			dir := ""
			switch {
			case ctx.Repo != "":
				dir = filepath.Join(ctx.Repo, "starter-wiki")
			case ctx.Home != "":
				dir = filepath.Join(ctx.Home, "starter-wiki")
				p.StarterWiki = dir
			default:
				p.fail("%s: can't tell where CMF is installed", f.Label)
				continue
			}
			value = strings.ReplaceAll(value, "{{starter_wiki}}", dir)
		}
		p.Env.Set(key, value)
	}
	for _, key := range w.Unset {
		p.Env.Unset(key)
	}
	for _, key := range sortedKeys(w.ListAdd) {
		items := List(p.Env.Get(key))
		if !p.Env.Has(key) {
			items = List(listDefault(m, key))
		}
		if !slices.Contains(items, w.ListAdd[key]) {
			items = append(items, w.ListAdd[key])
		}
		p.Env.Set(key, strings.Join(items, ","))
	}
	for _, key := range sortedKeys(w.ListRemove) {
		items := List(p.Env.Get(key))
		if !p.Env.Has(key) {
			items = List(listDefault(m, key))
		}
		items = slices.DeleteFunc(items, func(s string) bool { return s == w.ListRemove[key] })
		p.Env.Set(key, strings.Join(items, ","))
	}
	p.Stop = append(p.Stop, w.Stop...)
	if w == f.OnDisable {
		p.Disabled = append(p.Disabled, f.ID)
	}
}

// listDefault is a comma list's value when its key is unset, as the
// manifest's list_has conditions declare it (CMF_KNOWLEDGE_PROVIDERS=wiki).
func listDefault(m *Manifest, key string) string {
	var find func(c *Cond) string
	find = func(c *Cond) string {
		if c == nil {
			return ""
		}
		if c.ListHas != nil && c.ListHas.Key == key {
			return c.ListHas.Default
		}
		for _, list := range [][]Cond{c.All, c.Any} {
			for i := range list {
				if d := find(&list[i]); d != "" {
					return d
				}
			}
		}
		return find(c.Not)
	}
	for _, f := range m.Features {
		if d := find(f.EnabledWhen); d != "" {
			return d
		}
	}
	return ""
}

func sortedKeys(m map[string]string) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

func (m *Manifest) diff(old, new *home.Env) []Change {
	keys := map[string]bool{}
	for _, k := range old.Keys() {
		keys[k] = true
	}
	for _, k := range new.Keys() {
		keys[k] = true
	}
	var changes []Change
	for _, k := range sortedKeys(boolKeys(keys)) {
		o, n := old.Get(k), new.Get(k)
		if o == n && old.Has(k) == new.Has(k) {
			continue
		}
		changes = append(changes, Change{Key: k, Old: o, New: n, Removed: !new.Has(k), Secret: m.SecretKey(k)})
	}
	return changes
}

func boolKeys(m map[string]bool) map[string]string {
	out := map[string]string{}
	for k := range m {
		out[k] = ""
	}
	return out
}

// violations lists every rule the planned env breaks. current is the env
// before the change (for the lock rule).
func (m *Manifest) violations(current, planned *home.Env, ctx Context) []string {
	var out []string
	on := func(id string) bool { return m.Enabled(planned, id) }
	label := func(id string) string { return m.Feature(id).Label }
	for _, r := range m.Rules {
		switch r.Type {
		case RuleForbidValue:
			if !on(r.Feature) {
				continue
			}
			in := m.Feature(r.Feature).Input(r.Input)
			for _, v := range Values(planned, in) {
				if slices.Contains(r.Values, v) {
					out = append(out, message(r, fmt.Sprintf("%s can't be %s", in.Label, v)))
				}
			}
		case RuleExactlyOne:
			var enabled []string
			for _, id := range r.Features {
				if on(id) {
					enabled = append(enabled, label(id))
				}
			}
			if len(enabled) != 1 {
				detail := "none is on"
				if len(enabled) > 1 {
					detail = strings.Join(enabled, " and ") + " are both on"
				}
				out = append(out, message(r, "pick one")+" ("+detail+")")
			}
		case RuleConflicts:
			if on(r.Feature) && on(r.With) {
				out = append(out, message(r, label(r.Feature)+" and "+label(r.With)+" can't both be on"))
			}
		case RuleRequires:
			if on(r.Feature) && !on(r.With) {
				out = append(out, message(r, label(r.Feature)+" needs "+label(r.With)))
			}
		case RuleRequiresAny:
			if !on(r.Feature) {
				continue
			}
			if !slices.ContainsFunc(r.AnyOf, on) {
				var names []string
				for _, id := range r.AnyOf {
					names = append(names, label(id))
				}
				out = append(out, message(r, label(r.Feature)+" needs one of: "+strings.Join(names, ", ")))
			}
		case RuleNeedsKey:
			if !on(r.Feature) {
				continue
			}
			in := m.Feature(r.Feature).Input(r.Input)
			for _, v := range Values(planned, in) {
				keys := r.Keys[v]
				if len(keys) == 0 {
					continue
				}
				if !slices.ContainsFunc(keys, func(k string) bool { return !placeholder(planned.Get(k)) }) {
					out = append(out, message(r, fmt.Sprintf("%s uses %s, but %s isn't set", label(r.Feature), v, strings.Join(keys, " or "))))
				}
			}
		case RuleLocked:
			in := m.Feature(r.Feature).Input(r.Input)
			was, now := Value(current, in), Value(planned, in)
			if was == now {
				continue
			}
			switch {
			case ctx.Nodes > 0:
				out = append(out, fmt.Sprintf("%s: can't change %s from %s to %s; the graph has %d nodes. %s", label(r.Feature), strings.ToLower(in.Label), was, now, ctx.Nodes, r.Message))
			case ctx.Nodes < 0:
				out = append(out, fmt.Sprintf("%s: can't change %s from %s to %s while cmf can't confirm the graph is empty (run `cmf doctor`)", label(r.Feature), strings.ToLower(in.Label), was, now))
			}
		}
	}
	return out
}

func message(r Rule, fallback string) string {
	if r.Message != "" {
		return r.Message
	}
	return fallback
}
