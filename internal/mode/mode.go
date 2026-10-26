// Package mode records how this install runs the CMF server (plan section
// 3.8, item 3.9). Both modes share ~/.cmf, its .env, state and FalkorDB; the
// only difference is where the server and poller code comes from:
//
//   - standard: the published image (deploy/docker/compose.yml alone).
//   - developer: built from a checkout, via compose.override.yml (the
//     deploy/docker/compose.dev.yml overlay) and CMF_REPO in .env.
//
// The mode is written to mode.json. Without it, the mode is read from the
// machine: an override that builds from a checkout means developer.
package mode

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/tmargolis/context-memory-fabric/deploy/docker"
	"github.com/tmargolis/context-memory-fabric/internal/home"
)

const (
	Standard  = "standard"
	Developer = "developer"
)

type Mode struct {
	Mode string `json:"mode"`
	Repo string `json:"repo,omitempty"`
}

// State is the recorded mode plus what the files on disk say, so Status can
// show a mismatch (e.g. mode.json says standard but an override still builds
// from a checkout).
type State struct {
	Mode
	Source   string // "mode.json" or "detected"
	Detected Mode
}

func (s State) Consistent() bool {
	return s.Mode.Mode == s.Detected.Mode && (s.Mode.Mode != Developer || s.Mode.Repo == s.Detected.Repo)
}

var buildLine = regexp.MustCompile(`(?m)^\s+build:`)

// Detect reads the mode from the compose override and .env.
func Detect(h home.Home) Mode {
	data, err := os.ReadFile(h.OverrideFile())
	if err != nil || !buildLine.Match(data) {
		return Mode{Mode: Standard}
	}
	repo := ""
	if env, err := home.ReadEnv(h.EnvFile()); err == nil {
		repo = env.Get("CMF_REPO")
	}
	return Mode{Mode: Developer, Repo: repo}
}

func Load(h home.Home) (State, error) {
	state := State{Detected: Detect(h)}
	data, err := os.ReadFile(h.ModeFile())
	if errors.Is(err, os.ErrNotExist) {
		state.Mode, state.Source = state.Detected, "detected"
		return state, nil
	}
	if err != nil {
		return state, err
	}
	if err := json.Unmarshal(data, &state.Mode); err != nil {
		return state, fmt.Errorf("%s is not valid JSON: %w", h.ModeFile(), err)
	}
	if err := state.Mode.Validate(); err != nil {
		return state, fmt.Errorf("%s: %w", h.ModeFile(), err)
	}
	state.Source = "mode.json"
	return state, nil
}

func (m Mode) Validate() error {
	switch m.Mode {
	case Standard:
		return nil
	case Developer:
		if m.Repo == "" {
			return errors.New("developer mode needs the checkout's path (--repo)")
		}
		return nil
	default:
		return fmt.Errorf("unknown mode %q (standard or developer)", m.Mode)
	}
}

// CheckRepo makes sure a developer-mode repo is a CMF checkout the overlay
// can build from.
func CheckRepo(repo string) error {
	if !filepath.IsAbs(repo) {
		return fmt.Errorf("%s: use an absolute path", repo)
	}
	for _, need := range []string{"Dockerfile", filepath.Join("server", "mcp.py")} {
		if !home.Exists(filepath.Join(repo, need)) {
			return fmt.Errorf("%s doesn't look like a CMF checkout (no %s)", repo, need)
		}
	}
	return nil
}

// Change is one file Set writes; listed before anything is written so the
// CLI (and later the app's Review screen) can show it.
type Change struct {
	Path string
	Note string
	Data []byte
	Perm os.FileMode
}

// Plan lists the writes that switch h to m. It writes nothing.
func Plan(h home.Home, m Mode) ([]Change, error) {
	if err := m.Validate(); err != nil {
		return nil, err
	}
	if m.Mode == Developer {
		if err := CheckRepo(m.Repo); err != nil {
			return nil, err
		}
	}
	var changes []Change

	modeJSON, _ := json.MarshalIndent(m, "", "  ")
	modeJSON = append(modeJSON, '\n')
	if current, _ := os.ReadFile(h.ModeFile()); string(current) != string(modeJSON) {
		changes = append(changes, Change{Path: h.ModeFile(), Note: "record the mode", Data: modeJSON, Perm: 0o644})
	}

	// The stack itself: this cmf's own compose file (a newer cmf updates it).
	if current, _ := os.ReadFile(h.ComposeFile()); string(current) != string(docker.Compose) {
		note := "write the stack's compose file"
		if len(current) > 0 {
			note = "update the stack's compose file to this cmf's (backup kept)"
		}
		changes = append(changes, Change{Path: h.ComposeFile(), Note: note, Data: docker.Compose, Perm: 0o644})
	}

	existing, _ := os.ReadFile(h.OverrideFile())
	local := LocalVolumes(string(existing))
	override := ""
	if m.Mode == Developer {
		override = string(docker.ComposeDev)
	} else if local != "" {
		override = "# Standard mode: the published image. Only this install's own volumes are added here.\n"
	}
	if local != "" {
		override = strings.TrimRight(override, "\n") + "\n\n" + local
	}
	switch {
	case override == "" && len(existing) > 0:
		changes = append(changes, Change{Path: h.OverrideFile(), Note: "remove the developer overlay (backup kept)", Data: nil})
	case override != "" && override != string(existing):
		note := "write the developer overlay (builds from " + m.Repo + ")"
		if m.Mode == Standard {
			note = "drop the developer overlay; keep this install's volume settings"
		}
		changes = append(changes, Change{Path: h.OverrideFile(), Note: note, Data: []byte(override), Perm: 0o644})
	}

	if m.Mode == Developer {
		env, err := home.ReadEnv(h.EnvFile())
		if err != nil && !errors.Is(err, os.ErrNotExist) {
			return nil, err
		}
		if env == nil {
			env = &home.Env{}
		}
		if env.Get("CMF_REPO") != m.Repo {
			env.Set("CMF_REPO", m.Repo)
			changes = append(changes, Change{Path: h.EnvFile(), Note: "set CMF_REPO=" + m.Repo, Data: env.Bytes(), Perm: 0o600})
		}
	}
	return changes, nil
}

// PinImage adds to changes, for standard mode, the CMF_VERSION that makes the
// stack run the server image released with this cmf (instead of latest).
// A development build ("dev", "", or a local "<version>-dev") pins nothing.
func PinImage(h home.Home, changes []Change, version string) ([]Change, error) {
	version = strings.TrimPrefix(version, "v")
	if version == "" || version == "dev" || strings.HasSuffix(version, "-dev") {
		return changes, nil
	}
	env, err := home.ReadEnv(h.EnvFile())
	if err != nil && !errors.Is(err, os.ErrNotExist) {
		return nil, err
	}
	if env == nil {
		env = &home.Env{}
	}
	if env.Get("CMF_VERSION") == version {
		return changes, nil
	}
	env.Set("CMF_VERSION", version)
	return append(changes, Change{Path: h.EnvFile(), Note: "set CMF_VERSION=" + version + " (the server image released with this cmf)", Data: env.Bytes(), Perm: 0o600}), nil
}

// Apply backs up every file in changes, then writes them. It returns the
// backup so the caller can name it for `cmf restore`.
func Apply(h home.Home, changes []Change, now time.Time) (*home.Backup, error) {
	backup, err := h.NewBackup(now)
	if err != nil {
		return nil, err
	}
	for _, c := range changes {
		if err := backup.Save(c.Path); err != nil {
			return backup, fmt.Errorf("backing up %s: %w", c.Path, err)
		}
	}
	for _, c := range changes {
		if c.Data == nil {
			if err := os.Remove(c.Path); err != nil && !errors.Is(err, os.ErrNotExist) {
				return backup, err
			}
			continue
		}
		if err := home.WriteFile(c.Path, c.Data, c.Perm); err != nil {
			return backup, fmt.Errorf("writing %s: %w", c.Path, err)
		}
	}
	return backup, nil
}

var topLevelKey = regexp.MustCompile(`^[A-Za-z_][\w-]*:`)

// LocalVolumes returns the override's top-level `volumes:` section, with the
// comment lines just above it. scripts/consolidate_install.py puts the
// install's own FalkorDB volume there (attached as external); it must
// survive a mode switch or the stack would start on an empty graph.
func LocalVolumes(override string) string {
	lines := strings.Split(override, "\n")
	start := -1
	for i, line := range lines {
		if strings.HasPrefix(line, "volumes:") {
			start = i
			break
		}
	}
	if start < 0 {
		return ""
	}
	end := len(lines)
	for i := start + 1; i < len(lines); i++ {
		if topLevelKey.MatchString(lines[i]) {
			end = i
			break
		}
	}
	for start > 0 && strings.HasPrefix(lines[start-1], "#") {
		start--
	}
	return strings.TrimRight(strings.Join(lines[start:end], "\n"), "\n") + "\n"
}
