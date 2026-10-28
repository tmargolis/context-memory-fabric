package main

// cmf update: replace this cmf with the latest GitHub release, then hand off to
// the new one to pin the server image to its version, pull and restart the
// stack (Standard mode), and update the checkpoint hooks. The UI's Update
// button runs the same thing.

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/checks"
	"github.com/tmargolis/context-memory-fabric/internal/home"
	"github.com/tmargolis/context-memory-fabric/internal/mode"
	"github.com/tmargolis/context-memory-fabric/internal/run"
)

var (
	releaseAPI    = "https://api.github.com/repos/tmargolis/context-memory-fabric/releases/latest"
	releaseClient = &http.Client{Timeout: 2 * time.Minute}
	executable    = os.Executable // the cmf to replace; tests point it at a scratch file
)

// releaseInfo is what the Update button shows: this cmf against the latest release.
type releaseInfo struct {
	Current   string            `json:"current"`
	Latest    string            `json:"latest"`
	Available bool              `json:"available"` // the latest release is newer than this cmf
	DevBuild  bool              `json:"dev_build"` // a local build: it reports, never replaces itself
	URL       string            `json:"url"`
	assets    map[string]string // asset name -> download URL
}

func latestRelease(ctx context.Context) (*releaseInfo, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, releaseAPI, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("Accept", "application/vnd.github+json")
	req.Header.Set("User-Agent", "cmf/"+version)
	res, err := releaseClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("can't reach GitHub: %w", err)
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("GitHub answered %s for the latest release", res.Status)
	}
	var rel struct {
		Tag    string `json:"tag_name"`
		URL    string `json:"html_url"`
		Assets []struct {
			Name string `json:"name"`
			URL  string `json:"browser_download_url"`
		} `json:"assets"`
	}
	if err := json.NewDecoder(res.Body).Decode(&rel); err != nil {
		return nil, fmt.Errorf("reading the latest release: %w", err)
	}
	info := &releaseInfo{
		Current:  version,
		Latest:   strings.TrimPrefix(rel.Tag, "v"),
		DevBuild: strings.HasSuffix(version, "-dev"),
		URL:      rel.URL,
		assets:   map[string]string{},
	}
	for _, a := range rel.Assets {
		info.assets[a.Name] = a.URL
	}
	info.Available = newerVersion(info.Latest, checks.BaseVersion(version))
	return info, nil
}

// newerVersion reports whether a is a later x.y.z than b.
func newerVersion(a, b string) bool {
	pa, pb := strings.Split(a, "."), strings.Split(b, ".")
	for i := 0; i < len(pa) || i < len(pb); i++ {
		var x, y int
		if i < len(pa) {
			x, _ = strconv.Atoi(pa[i])
		}
		if i < len(pb) {
			y, _ = strconv.Atoi(pb[i])
		}
		if x != y {
			return x > y
		}
	}
	return false
}

// releaseAsset is the download for this machine, as the release workflow names it.
func releaseAsset(goos, goarch string) (string, error) {
	switch {
	case goos == "darwin":
		return "cmf-macos-universal", nil
	case goos == "linux" && (goarch == "amd64" || goarch == "arm64"):
		return "cmf-linux-" + goarch, nil
	case goos == "windows" && goarch == "amd64":
		return "cmf-windows.exe", nil
	}
	return "", fmt.Errorf("no cmf release is built for %s/%s", goos, goarch)
}

func download(ctx context.Context, url string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", "cmf/"+version)
	res, err := releaseClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer res.Body.Close()
	if res.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("download answered %s", res.Status)
	}
	return io.ReadAll(res.Body)
}

// verifySum checks data against its line in a SHA256SUMS file.
func verifySum(sums []byte, name string, data []byte) error {
	sum := sha256.Sum256(data)
	got := hex.EncodeToString(sum[:])
	for _, line := range strings.Split(string(sums), "\n") {
		fields := strings.Fields(line)
		if len(fields) == 2 && strings.TrimPrefix(fields[1], "*") == name {
			if fields[0] != got {
				return fmt.Errorf("%s doesn't match SHA256SUMS; nothing was changed", name)
			}
			return nil
		}
	}
	return fmt.Errorf("SHA256SUMS has no line for %s; nothing was changed", name)
}

// replaceBinary puts data at path as a new file rather than rewriting the old
// one in place: macOS kills a signed program whose file was overwritten.
func replaceBinary(path string, data []byte) error {
	tmp, err := os.CreateTemp(filepath.Dir(path), ".cmf-update-*")
	if err != nil {
		return fmt.Errorf("can't write next to %s: %w", path, err)
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	if err := os.Chmod(tmp.Name(), 0o755); err != nil {
		return err
	}
	if runtime.GOOS == "windows" { // a running .exe can be renamed but not replaced
		old := path + ".old"
		os.Remove(old)
		if err := os.Rename(path, old); err != nil {
			return err
		}
	}
	return os.Rename(tmp.Name(), path)
}

func runUpdate(args []string, out io.Writer) error {
	flags := flag.NewFlagSet("update", flag.ContinueOnError)
	flags.SetOutput(out)
	check := flags.Bool("check", false, "only say whether a newer release exists")
	yes := flags.Bool("yes", false, "don't ask before replacing this cmf")
	finish := flags.Bool("finish", false, "") // run by the new cmf after the swap
	if err := flags.Parse(args); err != nil {
		return err
	}
	if *finish {
		return finishUpdate(context.Background(), out)
	}
	_, err := installLatest(context.Background(), *check, *yes, out)
	return err
}

// installLatest replaces this cmf with the latest release when it is newer and
// returns the version it installed ("" when it changed nothing). Once the new
// binary is in place, a failure to finish still returns that version.
func installLatest(ctx context.Context, checkOnly, yes bool, out io.Writer) (string, error) {
	info, err := latestRelease(ctx)
	if err != nil {
		return "", err
	}
	fmt.Fprintf(out, "This cmf: %s. Latest release: %s.\n", info.Current, info.Latest)
	if !info.Available {
		fmt.Fprintln(out, "Up to date.")
		return "", nil
	}
	if checkOnly {
		fmt.Fprintln(out, "Run `cmf update` to install it.")
		return "", nil
	}
	if info.DevBuild {
		return "", fmt.Errorf("this is a development build (%s); it doesn't replace itself. Rebuild it from the repo, or download cmf %s from %s", version, info.Latest, info.URL)
	}
	name, err := releaseAsset(runtime.GOOS, runtime.GOARCH)
	if err != nil {
		return "", err
	}
	if info.assets[name] == "" || info.assets["SHA256SUMS"] == "" {
		return "", fmt.Errorf("release %s has no %s or SHA256SUMS; nothing was changed", info.Latest, name)
	}
	self, err := executable()
	if err != nil {
		return "", err
	}
	if self, err = filepath.EvalSymlinks(self); err != nil {
		return "", err
	}
	if !yes && !confirm(out, fmt.Sprintf("Replace %s with cmf %s?", self, info.Latest)) {
		return "", errors.New("cancelled; nothing was changed")
	}
	fmt.Fprintf(out, "Downloading %s %s…\n", name, info.Latest)
	data, err := download(ctx, info.assets[name])
	if err != nil {
		return "", fmt.Errorf("downloading %s: %w", name, err)
	}
	sums, err := download(ctx, info.assets["SHA256SUMS"])
	if err != nil {
		return "", fmt.Errorf("downloading SHA256SUMS: %w", err)
	}
	if err := verifySum(sums, name, data); err != nil {
		return "", err
	}
	fmt.Fprintln(out, "Checksum matches SHA256SUMS.")
	h, err := home.Default()
	if err != nil {
		return "", err
	}
	backup, err := h.NewBackup(now())
	if err != nil {
		return "", err
	}
	backup.SaveNote(fmt.Sprintf("cmf update %s → %s", version, info.Latest))
	if err := backup.Save(self); err != nil {
		return "", fmt.Errorf("backing up %s: %w", self, err)
	}
	if err := replaceBinary(self, data); err != nil {
		return "", fmt.Errorf("replacing %s: %w (the old cmf is in backup %s)", self, err, backup.Stamp)
	}
	fmt.Fprintf(out, "Replaced %s with cmf %s (old one in backup %s).\n", self, info.Latest, backup.Stamp)
	if r := runner.Run(ctx, run.Cmd{Name: self, Args: []string{"update", "--finish"}, Stream: out}); !r.OK() {
		return info.Latest, fmt.Errorf("cmf %s is installed, but finishing the update failed (%v); run `cmf update --finish` to retry", info.Latest, r.Err)
	}
	return info.Latest, nil
}

// finishUpdate runs in the new cmf: point the stack at this version's server
// image and bring the checkpoint hooks along.
func finishUpdate(ctx context.Context, out io.Writer) error {
	h, err := home.Default()
	if err != nil {
		return err
	}
	if m := mode.Detect(h); m.Mode != mode.Standard {
		fmt.Fprintf(out, "Developer mode: the server builds from %s; rebuild it there (docker compose up -d --build).\n", m.Repo)
	} else {
		changes, err := mode.PinImage(h, nil, version)
		if err != nil {
			return err
		}
		if len(changes) > 0 {
			if _, err := mode.Apply(h, changes, now()); err != nil {
				return err
			}
			fmt.Fprintln(out, changes[0].Note)
		}
		for _, args := range [][]string{{"pull"}, {"up", "-d"}} {
			cmd := run.Cmd{Name: "docker", Args: append([]string{"compose", "--project-directory", h.Dir}, args...), Timeout: 10 * time.Minute}
			fmt.Fprintln(out, "Running: "+cmd.String())
			if r := runner.Run(ctx, cmd); !r.OK() {
				return fmt.Errorf("%s failed: %s", cmd, r.Output())
			}
		}
	}
	env, err := loadEnv(h)
	if err != nil {
		return err
	}
	for i := 0; i < 60 && checks.BaseVersion(serverVersion(ctx, env)) != checks.BaseVersion(version); i++ {
		sleep(2 * time.Second)
	}
	user, err := userHome()
	if err != nil {
		return err
	}
	if err := updateHookBinaries(h, user, env, false, true, out); err != nil {
		fmt.Fprintln(out, "Checkpoint hooks not updated: "+err.Error())
	}
	fmt.Fprintf(out, "cmf %s is installed. Quit CMF management and open it again to use it.\n", version)
	return nil
}
