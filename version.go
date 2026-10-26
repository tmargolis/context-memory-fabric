package contextmemoryfabric

import (
	_ "embed"
	"regexp"
)

//go:embed pyproject.toml
var pyproject string

var versionLine = regexp.MustCompile(`(?m)^version = "([^"]+)"`)

// Version is the project's version from pyproject.toml: the one number the
// release workflow checks the tag, the cmf binary and the server image
// against, so a local build reports it too instead of "dev".
func Version() string {
	if m := versionLine.FindStringSubmatch(pyproject); m != nil {
		return m[1]
	}
	return "dev"
}
