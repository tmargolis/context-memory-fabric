package contextmemoryfabric

import (
	_ "embed"
	"encoding/json"
	"strings"
)

//go:embed docs/INSTRUCTIONS-FOR-AGENTS.md
var instructionsDoc string

//go:embed plugins/cmf-capture/.claude-plugin/plugin.json
var pluginManifest []byte

// AgentInstructions is the block an AI app's custom instructions take: the
// text fence in docs/INSTRUCTIONS-FOR-AGENTS.md, so the app's Copy button
// and the doc never drift apart.
func AgentInstructions() string {
	_, rest, ok := strings.Cut(instructionsDoc, "````text\n")
	if !ok {
		return ""
	}
	block, _, _ := strings.Cut(rest, "\n````")
	return strings.TrimSpace(block)
}

// PluginVersion is the cmf-capture plugin version this checkout ships.
func PluginVersion() string {
	var m struct {
		Version string `json:"version"`
	}
	json.Unmarshal(pluginManifest, &m)
	return m.Version
}
