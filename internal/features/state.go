package features

import (
	"fmt"
	"strings"

	"github.com/tmargolis/context-memory-fabric/internal/home"
)

func get(env *home.Env, key string) string {
	if env == nil {
		return ""
	}
	return env.Get(key)
}

// List splits a comma list such as COMPOSE_PROFILES.
func List(value string) []string {
	var out []string
	for _, item := range strings.Split(value, ",") {
		if item = strings.TrimSpace(item); item != "" {
			out = append(out, item)
		}
	}
	return out
}

func (m *Manifest) eval(env *home.Env, c *Cond, visiting map[string]int) (bool, error) {
	switch {
	case c.All != nil:
		for i := range c.All {
			if ok, err := m.eval(env, &c.All[i], visiting); err != nil || !ok {
				return false, err
			}
		}
		return true, nil
	case c.Any != nil:
		for i := range c.Any {
			if ok, err := m.eval(env, &c.Any[i], visiting); err != nil || ok {
				return ok, err
			}
		}
		return false, nil
	case c.Not != nil:
		ok, err := m.eval(env, c.Not, visiting)
		return !ok, err
	case c.Feature != "":
		return m.enabled(env, c.Feature, visiting)
	case c.EnvSet != "":
		return get(env, c.EnvSet) != "", nil
	case c.EnvSuffix != nil:
		return strings.HasSuffix(strings.TrimRight(get(env, c.EnvSuffix.Key), "/"), c.EnvSuffix.Suffix), nil
	case c.ListHas != nil:
		value := c.ListHas.Default
		if env != nil && env.Has(c.ListHas.Key) {
			value = env.Get(c.ListHas.Key)
		}
		for _, item := range List(value) {
			if item == c.ListHas.Item {
				return true, nil
			}
		}
		return false, nil
	}
	return false, fmt.Errorf("empty condition")
}

func (m *Manifest) enabled(env *home.Env, id string, visiting map[string]int) (bool, error) {
	f := m.Feature(id)
	if f == nil {
		return false, fmt.Errorf("unknown feature %q", id)
	}
	if f.AlwaysOn {
		return true, nil
	}
	if f.ComingSoon {
		return false, nil
	}
	if visiting[id] > 0 {
		return false, fmt.Errorf("feature %q's enabled_when refers back to itself", id)
	}
	visiting[id]++
	defer func() { visiting[id]-- }()
	return m.eval(env, f.EnabledWhen, visiting)
}

// Enabled reads a feature's state from env.
func (m *Manifest) Enabled(env *home.Env, id string) bool {
	ok, _ := m.enabled(env, id, map[string]int{})
	return ok
}

// Value is an input's current value: its env key, else (ranked) its
// first_env, else its default.
func Value(env *home.Env, in *Input) string {
	if v := get(env, in.Env); v != "" {
		return v
	}
	if in.FirstEnv != "" {
		if v := get(env, in.FirstEnv); v != "" {
			return v
		}
	}
	return in.Default
}

// Values is an input's value as a list (one item unless ranked).
func Values(env *home.Env, in *Input) []string {
	v := Value(env, in)
	if in.Type == TypeRanked {
		return List(v)
	}
	if v == "" {
		return nil
	}
	return []string{v}
}

type InputState struct {
	ID      string   `json:"id"`
	Label   string   `json:"label"`
	Type    string   `json:"type"`
	Env     string   `json:"env"`
	Value   string   `json:"value"` // masked for secrets
	IsSet   bool     `json:"set"`
	Choices []string `json:"choices,omitempty"`
}

type State struct {
	ID          string       `json:"id"`
	Group       string       `json:"group"`
	Label       string       `json:"label"`
	Description string       `json:"description"`
	Enabled     bool         `json:"enabled"`
	AlwaysOn    bool         `json:"always_on,omitempty"`
	Derived     bool         `json:"derived,omitempty"`
	ComingSoon  bool         `json:"coming_soon,omitempty"`
	Hidden      bool         `json:"hidden,omitempty"`
	Inputs      []InputState `json:"inputs,omitempty"`
	CostNote    string       `json:"cost_note,omitempty"`
}

// States reads every feature from env, secrets masked.
func (m *Manifest) States(env *home.Env) []State {
	var out []State
	for _, f := range m.Features {
		s := State{ID: f.ID, Group: f.Group, Label: f.Label, Description: f.Description,
			Enabled: m.Enabled(env, f.ID), AlwaysOn: f.AlwaysOn, Derived: f.Derived, ComingSoon: f.ComingSoon, Hidden: f.Hidden, CostNote: f.CostNote}
		for i := range f.Inputs {
			in := &f.Inputs[i]
			value := Value(env, in)
			shown := value
			if in.Type == TypeSecret {
				shown = Mask(value)
			}
			s.Inputs = append(s.Inputs, InputState{ID: in.ID, Label: in.Label, Type: in.Type, Env: in.Env,
				Value: shown, IsSet: get(env, in.Env) != "" || (in.FirstEnv != "" && get(env, in.FirstEnv) != ""), Choices: in.Choices})
		}
		out = append(out, s)
	}
	return out
}

// Mask shows that a secret is set without showing it.
func Mask(value string) string {
	switch {
	case value == "":
		return ""
	case len(value) <= 8:
		return "••••"
	}
	return "••••" + value[len(value)-4:]
}

// placeholder reports values that are set but aren't a real key, such as
// the old .env.example's `your_..._here`.
func placeholder(value string) bool {
	v := strings.ToLower(strings.TrimSpace(value))
	return v == "" || (strings.HasPrefix(v, "your_") && strings.HasSuffix(v, "_here")) || v == "changeme" || v == "xxx"
}

// Stops lists the compose services to stop when .env goes from before to
// after (features turned off whose on_disable stops a service), for changes
// made without a Plan, such as `cmf restore`.
func (m *Manifest) Stops(before, after *home.Env) []string {
	var out []string
	for _, f := range m.Features {
		if f.OnDisable != nil && m.Enabled(before, f.ID) && !m.Enabled(after, f.ID) {
			out = append(out, f.OnDisable.Stop...)
		}
	}
	return out
}
