package contextmemoryfabric

import (
	"os"
	"regexp"
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
