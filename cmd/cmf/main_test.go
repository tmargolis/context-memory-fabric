package main

import (
	"bytes"
	"strings"
	"testing"
)

func TestUnknownCommand(t *testing.T) {
	var out, errOut bytes.Buffer
	if code := run([]string{"frobnicate"}, &out, &errOut); code != 2 || !strings.Contains(errOut.String(), "unknown command") {
		t.Fatalf("code %d, %s", code, errOut.String())
	}
}

func TestModeShowAndDryRun(t *testing.T) {
	t.Setenv("CMF_HOME", t.TempDir())
	var out, errOut bytes.Buffer
	if code := run([]string{"mode"}, &out, &errOut); code != 0 || !strings.Contains(out.String(), "Mode: standard (from detected)") {
		t.Fatalf("code %d: %s %s", code, out.String(), errOut.String())
	}
	out.Reset()
	if code := run([]string{"mode", "set", "standard", "--dry-run"}, &out, &errOut); code != 0 || !strings.Contains(out.String(), "Dry run") {
		t.Fatalf("code %d: %s %s", code, out.String(), errOut.String())
	}
	out.Reset()
	if run([]string{"mode"}, &out, &errOut); !strings.Contains(out.String(), "from detected") {
		t.Fatal("dry run must not write mode.json")
	}
}
