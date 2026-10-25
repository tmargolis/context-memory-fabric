package home

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"
)

// BackupInfo describes one folder under backups/.
type BackupInfo struct {
	Stamp string
	// Kind is "cmf" (made by the cmf CLI: home/ and external/ trees),
	// "migrate" (made by cmf migrate), "consolidate" (made by
	// scripts/consolidate_install.py) or "unknown".
	Kind  string
	Files []string // paths a cmf restore would put back (or remove)
	Note  string   // backups/<stamp>/NOTE, when the change left one
}

func (h Home) Backups() ([]BackupInfo, error) {
	entries, err := os.ReadDir(h.BackupsDir())
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	var out []BackupInfo
	for _, e := range entries {
		if !e.IsDir() {
			continue
		}
		info, err := h.Backup(e.Name())
		if err != nil {
			return nil, err
		}
		out = append(out, info)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Stamp > out[j].Stamp })
	return out, nil
}

// Backup reads one backup folder.
func (h Home) Backup(stamp string) (BackupInfo, error) {
	info := BackupInfo{Stamp: stamp, Kind: "unknown"}
	if stamp == "" || strings.ContainsAny(stamp, `/\`) || stamp == "." || stamp == ".." {
		return info, fmt.Errorf("%q isn't a backup name", stamp)
	}
	dir := filepath.Join(h.BackupsDir(), stamp)
	if _, err := os.Stat(dir); err != nil {
		return info, fmt.Errorf("no backup %s in %s", stamp, h.BackupsDir())
	}
	if note, err := os.ReadFile(filepath.Join(dir, "NOTE")); err == nil {
		info.Note = strings.TrimSpace(string(note))
	}
	files, err := backedUp(h, dir)
	if err != nil {
		return info, err
	}
	switch {
	case len(files) > 0:
		info.Kind, info.Files = "cmf", files
	case Exists(filepath.Join(dir, "migrate.json")):
		info.Kind = "migrate"
	case Exists(filepath.Join(dir, "facts.json")) || Exists(filepath.Join(dir, "falkordb-dump.rdb")) || Exists(filepath.Join(dir, "repo.env")) || Exists(filepath.Join(dir, "compose.yml")):
		info.Kind = "consolidate"
	}
	return info, nil
}

// backedUp maps a cmf backup's home/ and external/ trees back to the paths
// they came from.
func backedUp(h Home, dir string) ([]string, error) {
	var files []string
	for _, tree := range []string{"home", "external"} {
		root := filepath.Join(dir, tree)
		if !Exists(root) {
			continue
		}
		err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
			if err != nil || d.IsDir() {
				return err
			}
			rel, _ := filepath.Rel(root, strings.TrimSuffix(path, ".absent"))
			if tree == "home" {
				files = append(files, filepath.Join(h.Dir, rel))
			} else {
				files = append(files, string(filepath.Separator)+rel)
			}
			return nil
		})
		if err != nil {
			return nil, err
		}
	}
	sort.Strings(files)
	return files, nil
}

// SaveNote leaves a one-line description of the change in the backup, shown
// by `cmf restore` when it lists backups.
func (b *Backup) SaveNote(note string) error {
	return os.WriteFile(filepath.Join(b.Dir(), "NOTE"), []byte(note+"\n"), 0o600)
}

// Restore puts a cmf backup's files back: each copy is written over the
// current file, and files recorded as absent are removed. When undo is set,
// the current files are backed up first (a new stamp), so the restore can
// itself be undone. It returns that new backup (nil without undo).
func (h Home) Restore(stamp string, undo bool, now time.Time) (*Backup, error) {
	info, err := h.Backup(stamp)
	if err != nil {
		return nil, err
	}
	switch info.Kind {
	case "migrate":
		return nil, fmt.Errorf("backup %s was made by `cmf migrate`; undo it with `cmf migrate restore %s`", stamp, stamp)
	case "consolidate":
		return nil, fmt.Errorf("backup %s was made by scripts/consolidate_install.py; undo it with `python scripts/consolidate_install.py restore %s`", stamp, stamp)
	case "unknown":
		return nil, fmt.Errorf("backup %s holds no files cmf can restore", stamp)
	}
	var current *Backup
	if undo {
		if current, err = h.NewBackup(now); err != nil {
			return nil, err
		}
		if current.Stamp == stamp {
			return nil, fmt.Errorf("backup %s was made this second; wait a moment and retry", stamp)
		}
		current.SaveNote("before restoring " + stamp)
		for _, path := range info.Files {
			if err := current.Save(path); err != nil {
				return current, fmt.Errorf("backing up %s before the restore: %w", path, err)
			}
		}
	}
	source := &Backup{Home: h, Stamp: stamp}
	for _, path := range info.Files {
		saved := source.destFor(path)
		if Exists(saved + ".absent") {
			if err := os.Remove(path); err != nil && !errors.Is(err, os.ErrNotExist) {
				return current, err
			}
			continue
		}
		data, err := os.ReadFile(saved)
		if err != nil {
			return current, err
		}
		stat, err := os.Stat(saved)
		if err != nil {
			return current, err
		}
		if err := WriteFile(path, data, stat.Mode().Perm()); err != nil {
			return current, fmt.Errorf("restoring %s: %w", path, err)
		}
	}
	return current, nil
}
