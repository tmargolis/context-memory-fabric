package diag

import (
	"fmt"
	"sort"
	"strings"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/checks"
	"github.com/tmargolis/context-memory-fabric/internal/probe"
)

// ErrorLimit is how many error lines a report carries.
const ErrorLimit = 50

// Shown lists the .env settings whose values a report shows: provider and
// feature switches. Every other key is listed as set or empty, and its value
// is redacted wherever it appears.
var Shown = map[string]bool{
	"COMPOSE_PROFILES": true, "EMBEDDING_DIM": true, "FALKORDB_DATABASE": true,
	"CMF_LLM_PROVIDER": true, "CMF_LLM_PROVIDERS": true, "CMF_EMBED_PROVIDER": true, "CMF_CAPTURE_LLM_PROVIDER": true,
	"CMF_LLM_SLOT": true, "CMF_LOCAL_LLM_MODEL": true, "CMF_LOCAL_EMBED_MODEL": true, "CMF_LOCAL_STRUCTURED_MODE": true,
	"CMF_KNOWLEDGE_PROVIDERS": true, "CMF_NOTION_PLAN": true, "CMF_CAPTURE_SOURCES": true,
	"CMF_EXTRACTION_PROFILE": true, "CMF_ENTITY_DEBRIS_FILTER": true,
}

// Hidden is the complement of Shown, for NewRedactor.
func Hidden(key string) bool { return !Shown[key] }

type Input struct {
	At       time.Time
	Version  string // cmf's
	Arch     string
	Facts    probe.Facts
	Results  []checks.Result
	Env      map[string]string // all of .env
	Logs     string            // compose log output to pick error lines from
	LogsErr  string
	Redactor *Redactor
}

// Build writes the report. Everything that comes from logs, the server or
// .env goes through the redactor.
func Build(in Input) string {
	r, f := in.Redactor, in.Facts
	var b strings.Builder
	line := func(format string, a ...any) { b.WriteString(r.Line(fmt.Sprintf(format, a...)) + "\n") }

	b.WriteString("CMF diagnostics\n")
	line("Made: %s", in.At.UTC().Format(time.RFC3339))
	docker := "not running"
	switch {
	case f.Docker.Responding:
		docker = f.Docker.Version
	case f.Docker.Hung:
		docker = "running, not answering"
	case !f.Docker.Installed:
		docker = "not installed"
	}
	line("cmf %s on %s/%s; Docker %s; mode %s", in.Version, f.OS, in.Arch, docker, orDash(f.Mode.Mode.Mode))

	b.WriteString("\nServer\n")
	if h := f.Health; h != nil {
		nodes := "unknown"
		if f.Nodes >= 0 {
			nodes = fmt.Sprint(f.Nodes)
		}
		line("  version %s, runtime %s, status %s", orDash(h.Version), orDash(h.Runtime), orDash(h.Status))
		line("  graph %s (%s nodes; FalkorDB reachable %t, graph exists %t)", orDash(h.Graph), nodes, h.FalkorDB.Reachable, h.FalkorDB.GraphExists)
		line("  providers: LLM %s, embeddings %s (%d dims), capture %s", orDash(h.Providers.LLM), orDash(h.Providers.Embeddings), h.Providers.EmbeddingDim, orDash(h.Providers.CaptureLLM))
		line("  knowledge sources: %s", orDash(strings.Join(h.KnowledgeSources, ", ")))
		for _, w := range h.BackgroundWrites {
			at := w.FinishedAt
			if at == "" {
				at = w.QueuedAt
			}
			line("  background write %s %s %s", at, w.Status, w.ErrorKind)
		}
		if c := h.ConfigCheck; c != nil {
			for _, p := range c.Problems {
				line("  config problem: %s", p)
			}
			for _, w := range c.Warnings {
				line("  config warning: %s", w)
			}
		}
		if t := h.Selftest; t != nil {
			line("  self-test %s ok=%t (%.0f s) %s %s", t.At.UTC().Format(time.RFC3339), t.OK, t.Seconds, t.FailedStage, t.FailedDetail)
		}
		for _, t := range h.ProviderTests {
			line("  provider test %s %s ok=%t: %s", t.Role, t.Provider, t.OK, t.Detail)
		}
		for _, c := range h.ClientsSeen {
			line("  client seen: %s, last %s", c.Harness, c.LastSeen.UTC().Format(time.RFC3339))
		}
	} else {
		line("  no /health answer: %s", orDash(f.HealthErr))
	}

	b.WriteString("\nSettings (.env)\n")
	keys := make([]string, 0, len(in.Env))
	for k := range in.Env {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	for _, k := range keys {
		v := in.Env[k]
		switch {
		case Shown[k]:
			line("  %s=%s", k, v)
		case strings.TrimSpace(v) == "":
			b.WriteString("  " + k + " (empty)\n")
		default:
			b.WriteString("  " + k + " (set)\n")
		}
	}

	b.WriteString("\nChecks\n")
	for _, c := range in.Results {
		line("  [%s] %s: %s", c.Level, c.Title, c.Detail)
	}

	b.WriteString("\nErrors in the stack's logs\n")
	errs := r.ErrorLines(in.Logs, ErrorLimit)
	switch {
	case in.LogsErr != "":
		line("  couldn't read the logs: %s", in.LogsErr)
	case len(errs) == 0:
		b.WriteString("  none\n")
	default:
		line("  last %d error lines:", len(errs))
		for _, e := range errs {
			b.WriteString("  " + e + "\n")
		}
	}
	b.WriteString("\nKeys, tokens, names, paths and memory text are removed. Nothing is sent anywhere by cmf.\n")
	return b.String()
}

func orDash(s string) string {
	if strings.TrimSpace(s) == "" {
		return "-"
	}
	return s
}
