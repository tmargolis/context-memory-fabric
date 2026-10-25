package home

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestEnvSetKeepsOtherLines(t *testing.T) {
	path := filepath.Join(t.TempDir(), ".env")
	original := "# keys\nOPENAI_API_KEY=sk-1\n\nexport CMF_REPO=/old # checkout\nCMF_REPO=/older\n"
	os.WriteFile(path, []byte(original), 0o600)
	env, err := ReadEnv(path)
	if err != nil {
		t.Fatal(err)
	}
	if got := env.Get("CMF_REPO"); got != "/older" {
		t.Fatalf("last value wins, got %q", got)
	}
	env.Set("CMF_REPO", "/new path")
	env.Set("NEW_KEY", "x")
	if err := env.Write(path); err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(path)
	want := "# keys\nOPENAI_API_KEY=sk-1\n\nexport CMF_REPO=/old # checkout\nCMF_REPO=\"/new path\"\nNEW_KEY=x\n"
	if string(data) != want {
		t.Fatalf("got\n%s\nwant\n%s", data, want)
	}
	info, _ := os.Stat(path)
	if info.Mode().Perm() != 0o600 {
		t.Fatalf(".env must be owner-only, got %v", info.Mode().Perm())
	}
	if keys := strings.Join(env.Keys(), ","); keys != "OPENAI_API_KEY,CMF_REPO,NEW_KEY" {
		t.Fatalf("keys %s", keys)
	}
	if env.Get("CMF_REPO") != "/new path" {
		t.Fatal("Set must change the value that takes effect (the last one)")
	}
	env.Unset("CMF_REPO")
	if env.Has("CMF_REPO") || !env.Has("NEW_KEY") {
		t.Fatal("Unset removes every copy, nothing else")
	}
}

func TestEnvQuotesAndComments(t *testing.T) {
	path := filepath.Join(t.TempDir(), ".env")
	os.WriteFile(path, []byte("A=\"x # not a comment\"\nB='y'\nC=z # comment\nnot a pair\n"), 0o600)
	env, _ := ReadEnv(path)
	for key, want := range map[string]string{"A": "x # not a comment", "B": "y", "C": "z", "D": ""} {
		if got := env.Get(key); got != want {
			t.Errorf("%s = %q, want %q", key, got, want)
		}
	}
}

func TestStateDirFollowsEnv(t *testing.T) {
	h := Home{Dir: t.TempDir()}
	if h.StateDir() != filepath.Join(h.Dir, "state") {
		t.Fatal("default state dir")
	}
	os.WriteFile(h.EnvFile(), []byte("CMF_STATE_DIR=/elsewhere\n"), 0o600)
	if h.StateDir() != "/elsewhere" {
		t.Fatal("CMF_STATE_DIR from .env")
	}
}

func TestDefaultUsesCMFHome(t *testing.T) {
	dir := t.TempDir()
	t.Setenv("CMF_HOME", dir)
	h, err := Default()
	if err != nil || h.Dir != dir {
		t.Fatalf("got %v %v", h, err)
	}
}

func TestBackupSave(t *testing.T) {
	h := Home{Dir: t.TempDir()}
	os.WriteFile(h.EnvFile(), []byte("A=1\n"), 0o600)
	outside := filepath.Join(t.TempDir(), "claude.json")
	os.WriteFile(outside, []byte("{}"), 0o644)

	b, err := h.NewBackup(time.Date(2026, 10, 24, 9, 30, 0, 0, time.UTC))
	if err != nil {
		t.Fatal(err)
	}
	if b.Stamp != "20261024-093000" {
		t.Fatalf("stamp %s", b.Stamp)
	}
	for _, p := range []string{h.EnvFile(), h.ModeFile(), outside} {
		if err := b.Save(p); err != nil {
			t.Fatal(err)
		}
	}
	if data, _ := os.ReadFile(filepath.Join(b.Dir(), "home", ".env")); string(data) != "A=1\n" {
		t.Fatal("home file copied under home/")
	}
	if !Exists(filepath.Join(b.Dir(), "home", "mode.json.absent")) {
		t.Fatal("missing file recorded as absent")
	}
	abs, _ := filepath.Abs(outside)
	if !Exists(filepath.Join(b.Dir(), "external", strings.TrimPrefix(abs, "/"))) {
		t.Fatal("outside file copied under external/")
	}
}
