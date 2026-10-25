// Package diag builds the redacted diagnostics report (`cmf diagnostics`,
// the app's Copy diagnostics) and picks error lines out of container logs.
// The report goes to Todd, so it carries no keys, tokens or memory content.
package diag

import (
	"regexp"
	"sort"
	"strings"
)

// MaxLine caps each log line in a report; long lines are where memory text
// and payloads end up.
const MaxLine = 400

// Redactor removes secrets and memory text from text bound for a report.
type Redactor struct {
	secrets []string // literal values to remove, longest first
	home    string
}

// NewRedactor takes the .env values and the user's home folder, which
// becomes "~". Every value that hidden reports (secrets, and settings such as
// names and paths that aren't on the report's allowlist) is removed wherever
// it appears.
func NewRedactor(env map[string]string, hidden func(key string) bool, home string) *Redactor {
	r := &Redactor{home: home}
	for k, v := range env {
		v = strings.TrimSpace(v)
		if hidden(k) && len(v) >= 6 {
			r.secrets = append(r.secrets, v)
		}
	}
	sort.Slice(r.secrets, func(i, j int) bool { return len(r.secrets[i]) > len(r.secrets[j]) })
	return r
}

var (
	// Key shapes from the providers CMF talks to, in case a key isn't in .env.
	keyShapes = regexp.MustCompile(`sk-[A-Za-z0-9_\-]{16,}|AIza[0-9A-Za-z_\-]{20,}|(?:ntn|secret)_[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9\-]{10,}|eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}`)
	bearer    = regexp.MustCompile(`(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}`)
	// name=value / "name": "value" for anything named like a credential.
	namedCred = regexp.MustCompile(`(?i)\b([A-Za-z_]*(?:api[_-]?key|token|secret|password|passwd|authorization|auth))(["']?\s*[:=]\s*["']?)([^\s"',;}&]+)`)
	email     = regexp.MustCompile(`[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}`)
	// Quoted text long enough to be a memory, a query or a document excerpt.
	// A single quote counts only when it doesn't sit inside a word (isn't).
	longQuote = regexp.MustCompile(`"(?:[^"\\]|\\.){24,}"|(^|[^\w])'(?:[^'\\]|\\.){24,}'`)
	quotedURL = regexp.MustCompile(`^"https?://[^\s"]*"$`)
)

// Line redacts one line and caps its length.
func (r *Redactor) Line(s string) string {
	for _, v := range r.secrets {
		s = strings.ReplaceAll(s, v, "[redacted]")
	}
	s = keyShapes.ReplaceAllString(s, "[redacted]")
	s = bearer.ReplaceAllString(s, "$1 [redacted]")
	s = namedCred.ReplaceAllStringFunc(s, func(m string) string {
		p := namedCred.FindStringSubmatch(m)
		if p[3] == "[redacted]" || strings.EqualFold(p[3], "bearer") || strings.EqualFold(p[3], "basic") {
			return m
		}
		return p[1] + p[2] + "[redacted]"
	})
	s = email.ReplaceAllString(s, "[email]")
	s = longQuote.ReplaceAllStringFunc(s, func(m string) string {
		if quotedURL.MatchString(m) {
			return m
		}
		lead := ""
		if m[0] != '"' && m[0] != '\'' {
			lead = m[:1]
		}
		return lead + "[text removed]"
	})
	if r.home != "" && r.home != "/" {
		s = strings.ReplaceAll(s, r.home, "~")
	}
	if len(s) > MaxLine {
		s = s[:MaxLine] + " …[cut]"
	}
	return s
}

// errorLine matches the lines worth sending (and the log viewer marks):
// Python logging levels, tracebacks, exceptions anywhere in the line, HTTP
// error statuses from a provider, and "X failed for/to/…" messages such as
// the poller's per-cycle "errors" list, and Node-style "[error]" tags (the
// FalkorDB Browser). WARNING lines and counters like
// `"failed": 0` are left out.
var errorLine = regexp.MustCompile(`\b(ERROR|CRITICAL|FATAL)\b|Traceback \(most recent call last\)|\b[\w.]*(Error|Exception): |\bError code: [45]\d\d\b|\b[Ff]ailed (for|to|with|while|on)\b|\bfailed: |\[(error|fatal)\]`)

// ErrorLines returns the last n error lines of compose log output, redacted.
func (r *Redactor) ErrorLines(logs string, n int) []string {
	var out []string
	for _, line := range strings.Split(logs, "\n") {
		if IsError(line) {
			out = append(out, r.Line(strings.TrimRight(line, "\r")))
		}
	}
	if len(out) > n {
		out = out[len(out)-n:]
	}
	return out
}

// IsError reports whether a log line is one ErrorLines would pick.
func IsError(line string) bool { return errorLine.MatchString(line) }
