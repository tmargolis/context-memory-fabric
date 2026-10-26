package contextmemoryfabric

import (
	"os"
	"regexp"
	"strings"
	"testing"
)

func TestVersionIsPyprojects(t *testing.T) {
	data, err := os.ReadFile("pyproject.toml")
	if err != nil {
		t.Fatal(err)
	}
	want := regexp.MustCompile(`(?m)^version = "([^"]+)"`).FindStringSubmatch(string(data))[1]
	if got := Version(); got != want || got == "dev" {
		t.Fatalf("Version() = %q, pyproject says %q", got, want)
	}
}

func TestAgentInstructionsAndPluginVersion(t *testing.T) {
	if got := AgentInstructions(); !strings.HasPrefix(got, "Use my Context Memory Fabric (CMF) connector") || strings.Contains(got, "````") {
		t.Fatalf("%.80q", got)
	}
	if PluginVersion() == "" {
		t.Fatal("no plugin version")
	}
}
