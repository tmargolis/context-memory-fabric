// Package features is the feature manifest (plan section 3.4, item 3.2) and
// the engine behind `cmf apply`: what each feature is, how its current state
// is read from ~/.cmf/.env, what turning it on or off writes, and the rules
// a combination must satisfy. Adding a feature is mostly a manifest entry.
package features

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"slices"
)

//go:embed manifest.json
var manifestJSON []byte

type Manifest struct {
	Version  int       `json:"version"`
	Groups   []string  `json:"groups"`
	Features []Feature `json:"features"`
	Rules    []Rule    `json:"rules"`
}

type Feature struct {
	ID          string   `json:"id"`
	Group       string   `json:"group"`
	Label       string   `json:"label"`
	Description string   `json:"description"`
	AlwaysOn    bool     `json:"always_on"`
	Derived     bool     `json:"derived"` // on or off as a consequence of other features
	EnabledWhen *Cond    `json:"enabled_when"`
	Inputs      []Input  `json:"inputs"`
	OnEnable    *Writes  `json:"on_enable"`
	OnDisable   *Writes  `json:"on_disable"`
	Checks      []string `json:"checks"`
	CostNote    string   `json:"cost_note"`
}

const (
	TypeText   = "text"
	TypeSecret = "secret"
	TypeURL    = "url"
	TypePath   = "path"
	TypeChoice = "choice"
	TypeRanked = "ranked"
)

type Input struct {
	ID       string   `json:"id"`
	Label    string   `json:"label"`
	Type     string   `json:"type"`
	Choices  []string `json:"choices"`
	Env      string   `json:"env"`
	FirstEnv string   `json:"first_env"` // ranked: also write the first item here
	Default  string   `json:"default"`
	Required bool     `json:"required"`
}

// Writes is what turning a feature on or off does to .env. List items are
// comma lists such as COMPOSE_PROFILES; Stop names compose services to stop
// (a profile that is removed doesn't stop its running service on its own).
type Writes struct {
	Set        map[string]string `json:"set"`
	Unset      []string          `json:"unset"`
	ListAdd    map[string]string `json:"list_add"`
	ListRemove map[string]string `json:"list_remove"`
	Stop       []string          `json:"stop"`
}

// Cond is how a feature's state is read from .env. Exactly one field is set.
type Cond struct {
	All       []Cond `json:"all"`
	Any       []Cond `json:"any"`
	Not       *Cond  `json:"not"`
	Feature   string `json:"feature"`
	EnvSet    string `json:"env_set"`
	EnvSuffix *struct {
		Key    string `json:"key"`
		Suffix string `json:"suffix"`
	} `json:"env_suffix"`
	ListHas *struct {
		Key     string `json:"key"`
		Item    string `json:"item"`
		Default string `json:"default"` // the list's value when the key is unset
	} `json:"list_has"`
}

type Rule struct {
	Type     string              `json:"type"`
	Feature  string              `json:"feature"`
	Input    string              `json:"input"`
	With     string              `json:"with"`
	Values   []string            `json:"values"`
	Features []string            `json:"features"`
	AnyOf    []string            `json:"any_of"`
	Keys     map[string][]string `json:"keys"`
	Message  string              `json:"message"`
}

const (
	RuleForbidValue = "forbid_value"
	RuleExactlyOne  = "exactly_one"
	RuleConflicts   = "conflicts"
	RuleRequires    = "requires"
	RuleRequiresAny = "requires_any"
	RuleNeedsKey    = "needs_key"
	RuleLocked      = "locked_when_graph_has_data"
)

// Load parses the embedded manifest and checks it is consistent.
func Load() (*Manifest, error) {
	return Parse(manifestJSON)
}

func Parse(data []byte) (*Manifest, error) {
	var m Manifest
	if err := json.Unmarshal(data, &m); err != nil {
		return nil, fmt.Errorf("feature manifest: %w", err)
	}
	if err := m.check(); err != nil {
		return nil, fmt.Errorf("feature manifest: %w", err)
	}
	return &m, nil
}

func (m *Manifest) Feature(id string) *Feature {
	for i := range m.Features {
		if m.Features[i].ID == id {
			return &m.Features[i]
		}
	}
	return nil
}

func (f *Feature) Input(id string) *Input {
	for i := range f.Inputs {
		if f.Inputs[i].ID == id {
			return &f.Inputs[i]
		}
	}
	return nil
}

var inputTypes = []string{TypeText, TypeSecret, TypeURL, TypePath, TypeChoice, TypeRanked}

func (m *Manifest) check() error {
	seen := map[string]bool{}
	for _, f := range m.Features {
		if f.ID == "" || seen[f.ID] {
			return fmt.Errorf("feature id %q is empty or repeated", f.ID)
		}
		seen[f.ID] = true
		if !slices.Contains(m.Groups, f.Group) {
			return fmt.Errorf("%s: unknown group %q", f.ID, f.Group)
		}
		switch {
		case f.AlwaysOn && (f.Derived || f.EnabledWhen != nil):
			return fmt.Errorf("%s: always_on features have no enabled_when", f.ID)
		case f.Derived && (f.EnabledWhen == nil || f.OnEnable != nil || f.OnDisable != nil || len(f.Inputs) > 0):
			return fmt.Errorf("%s: a derived feature has enabled_when and nothing else", f.ID)
		case !f.AlwaysOn && !f.Derived && (f.EnabledWhen == nil || f.OnEnable == nil || f.OnDisable == nil):
			return fmt.Errorf("%s: needs enabled_when, on_enable and on_disable", f.ID)
		}
		for _, in := range f.Inputs {
			if !slices.Contains(inputTypes, in.Type) || in.Env == "" {
				return fmt.Errorf("%s.%s: bad type %q or no env", f.ID, in.ID, in.Type)
			}
			if (in.Type == TypeChoice || in.Type == TypeRanked) && len(in.Choices) == 0 {
				return fmt.Errorf("%s.%s: no choices", f.ID, in.ID)
			}
		}
	}
	var refs func(c *Cond) error
	refs = func(c *Cond) error {
		if c == nil {
			return nil
		}
		if c.Feature != "" && !seen[c.Feature] {
			return fmt.Errorf("condition names unknown feature %q", c.Feature)
		}
		for i := range c.All {
			if err := refs(&c.All[i]); err != nil {
				return err
			}
		}
		for i := range c.Any {
			if err := refs(&c.Any[i]); err != nil {
				return err
			}
		}
		return refs(c.Not)
	}
	for _, f := range m.Features {
		if err := refs(f.EnabledWhen); err != nil {
			return fmt.Errorf("%s: %w", f.ID, err)
		}
	}
	for i, r := range m.Rules {
		ids := append(append([]string{r.Feature, r.With}, r.Features...), r.AnyOf...)
		for _, id := range ids {
			if id != "" && !seen[id] {
				return fmt.Errorf("rule %d (%s) names unknown feature %q", i, r.Type, id)
			}
		}
		if r.Input != "" && m.Feature(r.Feature).Input(r.Input) == nil {
			return fmt.Errorf("rule %d (%s) names unknown input %s.%s", i, r.Type, r.Feature, r.Input)
		}
		switch r.Type {
		case RuleForbidValue, RuleExactlyOne, RuleConflicts, RuleRequires, RuleRequiresAny, RuleNeedsKey, RuleLocked:
		default:
			return fmt.Errorf("rule %d: unknown type %q", i, r.Type)
		}
	}
	// Every feature's state must be computable (no condition cycles).
	for _, f := range m.Features {
		if _, err := m.enabled(nil, f.ID, map[string]int{}); err != nil {
			return err
		}
	}
	return nil
}
