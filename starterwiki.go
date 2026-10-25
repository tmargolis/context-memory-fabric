// Package contextmemoryfabric holds files the cmf binary carries with it.
package contextmemoryfabric

import "embed"

// StarterWiki is the fictional demo wiki, so the demo-wiki feature works on
// an install without a checkout (cmf writes it to ~/.cmf/starter-wiki).
//
//go:embed starter-wiki/*.md starter-wiki/*/*.md
var StarterWiki embed.FS
