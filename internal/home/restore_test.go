package home

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func TestRestoreRoundTrip(t *testing.T) {
	h := Home{Dir: t.TempDir()}
	os.WriteFile(h.EnvFile(), []byte("A=1\n"), 0o600)
	b, _ := h.NewBackup(time.Date(2026, 10, 24, 9, 0, 0, 0, time.UTC))
	b.SaveNote("cmf apply: turn on notion")
	b.Save(h.EnvFile())
	b.Save(h.ModeFile()) // absent

	os.WriteFile(h.EnvFile(), []byte("A=2\n"), 0o600)
	os.WriteFile(h.ModeFile(), []byte(`{"mode":"standard"}`), 0o644)

	undo, err := h.Restore(b.Stamp, true, time.Date(2026, 10, 24, 9, 5, 0, 0, time.UTC))
	if err != nil {
		t.Fatal(err)
	}
	if data, _ := os.ReadFile(h.EnvFile()); string(data) != "A=1\n" {
		t.Fatalf(".env %q", data)
	}
	if Exists(h.ModeFile()) {
		t.Fatal("a file absent at backup time is removed")
	}
	if info, _ := os.Stat(h.EnvFile()); info.Mode().Perm() != 0o600 {
		t.Fatal("permissions kept")
	}
	// The restore is itself undoable.
	if _, err := h.Restore(undo.Stamp, false, time.Now()); err != nil {
		t.Fatal(err)
	}
	if data, _ := os.ReadFile(h.EnvFile()); string(data) != "A=2\n" || !Exists(h.ModeFile()) {
		t.Fatal("undo of the restore")
	}

	list, err := h.Backups()
	if err != nil || len(list) != 2 || list[0].Stamp != undo.Stamp || list[1].Note != "cmf apply: turn on notion" {
		t.Fatalf("%+v %v", list, err)
	}
	if len(list[1].Files) != 2 || !strings.HasSuffix(list[1].Files[1], "mode.json") {
		t.Fatalf("%+v", list[1].Files)
	}
}

func TestRestoreRefusesConsolidationBackups(t *testing.T) {
	h := Home{Dir: t.TempDir()}
	dir := filepath.Join(h.BackupsDir(), "20261023-164606")
	os.MkdirAll(filepath.Join(dir, "state"), 0o700)
	os.WriteFile(filepath.Join(dir, "facts.json"), []byte("{}"), 0o600)
	_, err := h.Restore("20261023-164606", true, time.Now())
	if err == nil || !strings.Contains(err.Error(), "consolidate_install.py restore 20261023-164606") {
		t.Fatal(err)
	}
	if _, err := h.Restore("../etc", false, time.Now()); err == nil {
		t.Fatal("stamps can't leave backups/")
	}
}
