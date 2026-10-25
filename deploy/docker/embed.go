// Package docker carries the stack's compose files inside the cmf binary, so
// Standard mode needs no checkout (docs/setup-simplification-plan.md, 3.2).
package docker

import _ "embed"

// Compose is deploy/docker/compose.yml, written to ~/.cmf/compose.yml.
//
//go:embed compose.yml
var Compose []byte

// ComposeDev is deploy/docker/compose.dev.yml, the Developer mode overlay
// written to ~/.cmf/compose.override.yml.
//
//go:embed compose.dev.yml
var ComposeDev []byte
