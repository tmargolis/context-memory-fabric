package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/tmargolis/context-memory-fabric/internal/run"
)

const stackLogs = `cmf-server  | INFO started
cmf-server  | ERROR recall failed for AIzaSyFAKEFAKEFAKEFAKEFAKE0000
cmf-poller  | INFO polled
`

func logsMachine(t *testing.T) (*run.Fake, string) {
	fake := fakeMachine(t, true)
	dir := os.Getenv("CMF_HOME")
	os.WriteFile(filepath.Join(dir, ".env"), []byte("COMPOSE_PROFILES=capture\nGEMINI_API_KEY=AIzaSyFAKEFAKEFAKEFAKEFAKE0000\nCMF_EXTRACTION_PERSON=Pat Example\n"), 0o600)
	fake.Answers["docker compose --project-directory "+dir+" logs*"] = run.Result{Stdout: stackLogs}
	userHome = func() (string, error) { return filepath.Dir(dir), nil }
	t.Cleanup(func() { userHome = os.UserHomeDir })
	return fake, dir
}

func TestLogs(t *testing.T) {
	fake, dir := logsMachine(t)
	var out, errOut bytes.Buffer
	if code := runCLI([]string{"logs", "cmf-server", "--tail", "50"}, &out, &errOut); code != 0 || out.String() != stackLogs {
		t.Fatalf("code %d\n%q%s", code, out.String(), errOut.String())
	}
	if !fake.Ran("docker compose --project-directory " + dir + " logs --no-color --tail 50 cmf-server") {
		t.Fatal(fake.Calls)
	}
	out.Reset()
	runCLI([]string{"logs", "--errors"}, &out, &errOut)
	if out.String() != "cmf-server  | ERROR recall failed for AIzaSyFAKEFAKEFAKEFAKEFAKE0000\n" {
		t.Fatalf("%q", out.String())
	}
	for _, bad := range [][]string{{"logs", "--tail", "x"}, {"logs", "--bogus"}, {"logs", "-f", "--errors"}} {
		if code := runCLI(bad, &out, &errOut); code != 1 {
			t.Errorf("%v: code %d", bad, code)
		}
	}
}

func TestDiagnosticsShowsThenCopiesOnlyOnYes(t *testing.T) {
	fake, _ := logsMachine(t)
	fake.Answers["pbcopy"] = run.Result{}
	t.Cleanup(func() { stdin = os.Stdin })

	stdin = strings.NewReader("n\n")
	var out, errOut bytes.Buffer
	if code := runCLI([]string{"diagnostics", "--copy"}, &out, &errOut); code != 0 {
		t.Fatalf("code %d %s", code, errOut.String())
	}
	report := out.String()
	for _, secret := range []string{"AIzaSyFAKEFAKEFAKEFAKEFAKE0000", "Pat Example", os.Getenv("CMF_HOME")} {
		if strings.Contains(report, secret) {
			t.Errorf("report has %q:\n%s", secret, report)
		}
	}
	if !strings.Contains(report, "in ~/"+filepath.Base(os.Getenv("CMF_HOME"))) || !strings.Contains(report, "ERROR recall failed for [redacted]") || !strings.Contains(report, "Not copied.") || fake.Ran("pbcopy") {
		t.Fatalf("%s", report)
	}

	stdin = strings.NewReader("y\n")
	out.Reset()
	runCLI([]string{"diagnostics", "--copy"}, &out, &errOut)
	var copied string
	for _, c := range fake.Calls {
		if c.Name == "pbcopy" {
			copied = c.Stdin
		}
	}
	if !strings.HasPrefix(copied, "CMF diagnostics\n") || strings.Contains(copied, "AIzaSy") || !strings.Contains(out.String(), "Copied.") {
		t.Fatalf("copied %q\n%s", copied, out.String())
	}
}

func TestDiagnosticsOut(t *testing.T) {
	logsMachine(t)
	file := filepath.Join(t.TempDir(), "report.txt")
	var out, errOut bytes.Buffer
	if code := runCLI([]string{"diagnostics", "--out", file}, &out, &errOut); code != 0 {
		t.Fatal(errOut.String())
	}
	info, err := os.Stat(file)
	if err != nil || info.Mode().Perm() != 0o600 {
		t.Fatal(info, err)
	}
	if code := runCLI([]string{"diagnostics", "--yes"}, &out, &errOut); code != 1 {
		t.Fatal("--yes without --copy")
	}
}

func TestSelftestCommand(t *testing.T) {
	fake, dir := logsMachine(t)
	key := "docker compose --project-directory " + dir + " exec -T cmf-server python -m server.selftest"
	fake.Answers[key] = run.Result{}
	var out, errOut bytes.Buffer
	if code := runCLI([]string{"selftest"}, &out, &errOut); code != 0 || !fake.Ran(key) {
		t.Fatalf("code %d %s", code, errOut.String())
	}
	var streamed bool
	for _, c := range fake.Calls {
		streamed = streamed || (c.String() == key && c.Stream != nil)
	}
	if !streamed {
		t.Fatal("selftest output should stream")
	}
}
