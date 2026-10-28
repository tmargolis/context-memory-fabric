package main

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"

	"github.com/tmargolis/context-memory-fabric/internal/run"
)

func TestNewerVersion(t *testing.T) {
	for _, c := range []struct {
		a, b string
		want bool
	}{{"1.0.3", "1.0.2", true}, {"1.0.2", "1.0.2", false}, {"1.0.10", "1.0.9", true}, {"1.1", "1.0.9", true}, {"0.9.9", "1.0.0", false}} {
		if got := newerVersion(c.a, c.b); got != c.want {
			t.Errorf("newerVersion(%q, %q) = %v", c.a, c.b, got)
		}
	}
}

func TestReleaseAssetNamesMatchTheWorkflow(t *testing.T) {
	for goos, want := range map[string]string{"darwin": "cmf-macos-universal", "windows": "cmf-windows.exe"} {
		if got, _ := releaseAsset(goos, "amd64"); got != want {
			t.Errorf("%s: %s", goos, got)
		}
	}
	if got, _ := releaseAsset("linux", "arm64"); got != "cmf-linux-arm64" {
		t.Error(got)
	}
	if _, err := releaseAsset("freebsd", "amd64"); err == nil {
		t.Error("freebsd has no release")
	}
}

func TestVerifySum(t *testing.T) {
	data := []byte("binary")
	sum := sha256.Sum256(data)
	sums := []byte(hex.EncodeToString(sum[:]) + "  cmf-macos-universal\n")
	if err := verifySum(sums, "cmf-macos-universal", data); err != nil {
		t.Fatal(err)
	}
	if err := verifySum(sums, "cmf-macos-universal", []byte("tampered")); err == nil || !strings.Contains(err.Error(), "doesn't match") {
		t.Fatal(err)
	}
	if err := verifySum(sums, "cmf-linux-amd64", data); err == nil || !strings.Contains(err.Error(), "no line") {
		t.Fatal(err)
	}
}

func TestReplaceBinaryWritesANewFile(t *testing.T) {
	path := filepath.Join(t.TempDir(), "cmf")
	os.WriteFile(path, []byte("old"), 0o755)
	before, _ := os.Stat(path)
	if err := replaceBinary(path, []byte("new")); err != nil {
		t.Fatal(err)
	}
	after, _ := os.Stat(path)
	if got, _ := os.ReadFile(path); string(got) != "new" || after.Mode().Perm() != 0o755 {
		t.Fatalf("%q %v", got, after.Mode())
	}
	if os.SameFile(before, after) {
		t.Fatal("rewritten in place: macOS would kill it")
	}
	if left, _ := filepath.Glob(filepath.Join(filepath.Dir(path), ".cmf-update-*")); len(left) != 0 {
		t.Fatal(left)
	}
}

// fakeRelease serves a latest release with this machine's asset and SHA256SUMS.
func fakeRelease(t *testing.T, tag string, binary []byte, sumOf []byte) {
	name, _ := releaseAsset(runtime.GOOS, runtime.GOARCH)
	sum := sha256.Sum256(sumOf)
	var srv *httptest.Server
	srv = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch r.URL.Path {
		case "/latest":
			json.NewEncoder(w).Encode(map[string]any{"tag_name": tag, "html_url": "https://example.test/" + tag, "assets": []map[string]string{
				{"name": name, "browser_download_url": srv.URL + "/bin"},
				{"name": "SHA256SUMS", "browser_download_url": srv.URL + "/sums"},
			}})
		case "/bin":
			w.Write(binary)
		case "/sums":
			w.Write([]byte(hex.EncodeToString(sum[:]) + "  " + name + "\n"))
		}
	}))
	t.Cleanup(srv.Close)
	old := releaseAPI
	releaseAPI = srv.URL + "/latest"
	t.Cleanup(func() { releaseAPI = old })
}

func withVersion(t *testing.T, v string) {
	old := version
	version = v
	t.Cleanup(func() { version = old })
}

func scratchSelf(t *testing.T) string {
	self := filepath.Join(t.TempDir(), "cmf")
	os.WriteFile(self, []byte("old cmf"), 0o755)
	executable = func() (string, error) { return self, nil }
	t.Cleanup(func() { executable = os.Executable })
	return self
}

func TestUpdateReplacesThisCmfAndHandsOff(t *testing.T) {
	fake := fakeMachine(t, true)
	withVersion(t, "1.0.2")
	self := scratchSelf(t)
	self, _ = filepath.EvalSymlinks(self)
	fake.Answers[self+" update --finish"] = run.Result{}
	fakeRelease(t, "v1.0.3", []byte("new cmf"), []byte("new cmf"))

	var out bytes.Buffer
	installed, err := installLatest(context.Background(), false, true, &out)
	if err != nil || installed != "1.0.3" {
		t.Fatalf("%q %v\n%s", installed, err, out.String())
	}
	if got, _ := os.ReadFile(self); string(got) != "new cmf" {
		t.Fatalf("%q", got)
	}
	if !fake.Ran(self+" update --finish") || !strings.Contains(out.String(), "Checksum matches") {
		t.Fatal(out.String())
	}
	backups, _ := filepath.Glob(filepath.Join(os.Getenv("CMF_HOME"), "backups", "*", "external", "*"))
	if len(backups) == 0 {
		t.Fatal("the old cmf wasn't backed up")
	}
}

func TestUpdateRefusesABadChecksum(t *testing.T) {
	fakeMachine(t, true)
	withVersion(t, "1.0.2")
	self := scratchSelf(t)
	fakeRelease(t, "v1.0.3", []byte("tampered"), []byte("new cmf"))
	if _, err := installLatest(context.Background(), false, true, &bytes.Buffer{}); err == nil || !strings.Contains(err.Error(), "doesn't match") {
		t.Fatal(err)
	}
	if got, _ := os.ReadFile(self); string(got) != "old cmf" {
		t.Fatalf("%q", got)
	}
}

func TestUpdateLeavesUpToDateAndDevBuildsAlone(t *testing.T) {
	fakeMachine(t, true)
	self := scratchSelf(t)
	fakeRelease(t, "v1.0.3", []byte("new cmf"), []byte("new cmf"))

	withVersion(t, "1.0.3")
	var out bytes.Buffer
	if installed, err := installLatest(context.Background(), false, true, &out); err != nil || installed != "" || !strings.Contains(out.String(), "Up to date.") {
		t.Fatalf("%q %v %s", installed, err, out.String())
	}
	withVersion(t, "1.0.2-dev")
	if _, err := installLatest(context.Background(), false, true, &bytes.Buffer{}); err == nil || !strings.Contains(err.Error(), "development build") {
		t.Fatal(err)
	}
	if got, _ := os.ReadFile(self); string(got) != "old cmf" {
		t.Fatalf("%q", got)
	}
}

func TestUIUpdateCheckHighlightsANewerRelease(t *testing.T) {
	_, h, _ := uiFixture(t)
	withVersion(t, "1.0.2")
	fakeRelease(t, "v1.0.3", nil, nil)
	w := call(h, "GET", "/api/update", "", nil)
	var got map[string]any
	json.Unmarshal(w.Body.Bytes(), &got)
	if w.Code != 200 || got["available"] != true || got["latest"] != "1.0.3" || got["current"] != "1.0.2" {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
}
