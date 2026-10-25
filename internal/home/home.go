// Package home is the one folder CMF lives in: ~/.cmf (CMF_HOME), holding
// .env, state/, the compose files, mode.json and dated backups
// (docs/setup-simplification-plan.md, section 3.8). It follows the same rules
// as server/core/env.py, so the CLI and the server agree on every path.
package home

import (
	"bufio"
	"bytes"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"time"
)

// StampFormat matches scripts/consolidate_install.py: 20261023-164606.
const StampFormat = "20060102-150405"

type Home struct {
	Dir string
}

// Default is $CMF_HOME, else ~/.cmf.
func Default() (Home, error) {
	if dir := os.Getenv("CMF_HOME"); dir != "" {
		return Home{Dir: expand(dir)}, nil
	}
	user, err := os.UserHomeDir()
	if err != nil {
		return Home{}, fmt.Errorf("can't find your home folder: %w", err)
	}
	return Home{Dir: filepath.Join(user, ".cmf")}, nil
}

func expand(path string) string {
	if path == "~" || strings.HasPrefix(path, "~/") {
		if user, err := os.UserHomeDir(); err == nil {
			return filepath.Join(user, strings.TrimPrefix(path, "~"))
		}
	}
	return path
}

func (h Home) Path(parts ...string) string {
	return filepath.Join(append([]string{h.Dir}, parts...)...)
}

func (h Home) EnvFile() string      { return h.Path(".env") }
func (h Home) ComposeFile() string  { return h.Path("compose.yml") }
func (h Home) OverrideFile() string { return h.Path("compose.override.yml") }
func (h Home) ModeFile() string     { return h.Path("mode.json") }
func (h Home) BackupsDir() string   { return h.Path("backups") }

// StateDir is CMF_STATE_DIR from .env when set, else <home>/state.
func (h Home) StateDir() string {
	if env, err := ReadEnv(h.EnvFile()); err == nil {
		if state := env.Get("CMF_STATE_DIR"); state != "" {
			return expand(state)
		}
	}
	return h.Path("state")
}

func Exists(path string) bool {
	_, err := os.Stat(path)
	return err == nil
}

// Backup is one dated folder under backups/: every file a change touches is
// copied here first, so `cmf restore <stamp>` can put it back.
type Backup struct {
	Home  Home
	Stamp string
}

func (h Home) NewBackup(now time.Time) (*Backup, error) {
	stamp := now.Format(StampFormat)
	if err := os.MkdirAll(filepath.Join(h.BackupsDir(), stamp), 0o700); err != nil {
		return nil, err
	}
	return &Backup{Home: h, Stamp: stamp}, nil
}

func (b *Backup) Dir() string { return filepath.Join(b.Home.BackupsDir(), b.Stamp) }

// Save copies path into the backup, keeping its place relative to the home
// folder (files outside it go under external/ with their absolute path).
// A missing file is recorded as absent, so restore knows to remove it.
func (b *Backup) Save(path string) error {
	dest := b.destFor(path)
	if err := os.MkdirAll(filepath.Dir(dest), 0o700); err != nil {
		return err
	}
	if !Exists(path) {
		return os.WriteFile(dest+".absent", nil, 0o600)
	}
	return copyFile(path, dest)
}

func (b *Backup) destFor(path string) string {
	abs, _ := filepath.Abs(path)
	if rel, err := filepath.Rel(b.Home.Dir, abs); err == nil && !strings.HasPrefix(rel, "..") {
		return filepath.Join(b.Dir(), "home", rel)
	}
	return filepath.Join(b.Dir(), "external", strings.TrimPrefix(abs, string(filepath.Separator)))
}

func copyFile(src, dst string) error {
	in, err := os.Open(src)
	if err != nil {
		return err
	}
	defer in.Close()
	info, err := in.Stat()
	if err != nil {
		return err
	}
	out, err := os.OpenFile(dst, os.O_CREATE|os.O_WRONLY|os.O_TRUNC, info.Mode().Perm())
	if err != nil {
		return err
	}
	if _, err := io.Copy(out, in); err != nil {
		out.Close()
		return err
	}
	return out.Close()
}

// WriteFile writes data atomically (temp file + rename) with perm.
func WriteFile(path string, data []byte, perm os.FileMode) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(filepath.Dir(path), "."+filepath.Base(path)+".tmp-*")
	if err != nil {
		return err
	}
	cleanup := func(err error) error {
		tmp.Close()
		os.Remove(tmp.Name())
		return err
	}
	if _, err := tmp.Write(data); err != nil {
		return cleanup(err)
	}
	if err := tmp.Chmod(perm); err != nil {
		return cleanup(err)
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmp.Name())
		return err
	}
	return os.Rename(tmp.Name(), path)
}

// Env is a .env file kept line by line, so setting one key leaves the
// comments, order and every other line exactly as they were.
type Env struct {
	lines []string
}

func ReadEnv(path string) (*Env, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	return parseEnv(f)
}

// ParseEnv reads .env text already in memory.
func ParseEnv(data []byte) (*Env, error) { return parseEnv(bytes.NewReader(data)) }

func parseEnv(r io.Reader) (*Env, error) {
	env := &Env{}
	scanner := bufio.NewScanner(r)
	scanner.Buffer(make([]byte, 64*1024), 1024*1024)
	for scanner.Scan() {
		env.lines = append(env.lines, scanner.Text())
	}
	return env, scanner.Err()
}

func parseLine(line string) (key, value string, ok bool) {
	trimmed := strings.TrimSpace(line)
	if trimmed == "" || strings.HasPrefix(trimmed, "#") {
		return "", "", false
	}
	trimmed = strings.TrimPrefix(trimmed, "export ")
	key, value, ok = strings.Cut(trimmed, "=")
	if !ok {
		return "", "", false
	}
	key = strings.TrimSpace(key)
	value = strings.TrimSpace(value)
	if len(value) >= 2 && (value[0] == '"' || value[0] == '\'') && value[len(value)-1] == value[0] {
		value = value[1 : len(value)-1]
	} else if i := strings.Index(value, " #"); i >= 0 {
		value = strings.TrimSpace(value[:i])
	}
	return key, value, true
}

// Get returns the last value set for key (as python-dotenv does), or "".
func (e *Env) Get(key string) string {
	value := ""
	for _, line := range e.lines {
		if k, v, ok := parseLine(line); ok && k == key {
			value = v
		}
	}
	return value
}

// Keys lists the keys set in the file, in order, once each.
func (e *Env) Keys() []string {
	seen := map[string]bool{}
	var keys []string
	for _, line := range e.lines {
		if k, _, ok := parseLine(line); ok && !seen[k] {
			seen[k] = true
			keys = append(keys, k)
		}
	}
	return keys
}

// Set replaces the key's line in place, or appends it. When the key is
// repeated it replaces the last line, the one that takes effect (dotenv and
// compose both let the last value win).
func (e *Env) Set(key, value string) {
	line := key + "=" + quote(value)
	for i := len(e.lines) - 1; i >= 0; i-- {
		if k, _, ok := parseLine(e.lines[i]); ok && k == key {
			e.lines[i] = line
			return
		}
	}
	e.lines = append(e.lines, line)
}

// Unset removes every line that sets key.
func (e *Env) Unset(key string) {
	kept := e.lines[:0:0]
	for _, line := range e.lines {
		if k, _, ok := parseLine(line); ok && k == key {
			continue
		}
		kept = append(kept, line)
	}
	e.lines = kept
}

// Has reports whether key is set at all (even to "").
func (e *Env) Has(key string) bool {
	for _, line := range e.lines {
		if k, _, ok := parseLine(line); ok && k == key {
			return true
		}
	}
	return false
}

func (e *Env) Clone() *Env {
	return &Env{lines: append([]string(nil), e.lines...)}
}

func quote(value string) string {
	if strings.ContainsAny(value, " #\"'") {
		return `"` + strings.ReplaceAll(value, `"`, `\"`) + `"`
	}
	return value
}

func (e *Env) Bytes() []byte {
	return []byte(strings.Join(e.lines, "\n") + "\n")
}

// Write saves the file owner-only (it holds keys; plan section 3.6).
func (e *Env) Write(path string) error {
	return WriteFile(path, e.Bytes(), 0o600)
}
