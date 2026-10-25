package diag

import (
	"strings"
	"testing"
	"time"

	"github.com/tmargolis/context-memory-fabric/internal/checks"
	"github.com/tmargolis/context-memory-fabric/internal/probe"
)

var env = map[string]string{
	"GEMINI_API_KEY":          "AIzaSyD-not-a-real-key-123456789",
	"CMF_NOTION_TOKEN":        "ntn_0123456789abcdefghijABCDEFGHIJ",
	"CMF_MCP_OAUTH_PASSWORD":  "hunter2-but-longer",
	"CMF_EXTRACTION_PERSON":   "Pat Example",
	"CMF_LOCAL_BASE_URL":      "http://spark.tail1234.ts.net:8080/v1",
	"CMF_KNOWLEDGE_PROVIDERS": "notion",
	"COMPOSE_PROFILES":        "spark,capture",
	"CMF_LLM_PROVIDER":        "local",
	"EMPTY_ONE":               "",
}

func secret(k string) bool {
	return strings.Contains(k, "KEY") || strings.Contains(k, "TOKEN") || strings.Contains(k, "PASSWORD")
}

func redactor() *Redactor {
	return NewRedactor(env, func(k string) bool { return secret(k) || Hidden(k) }, "/Users/pat")
}

func TestLineRemovesSecretsNamesAndText(t *testing.T) {
	r := redactor()
	cases := map[string]string{
		"auth failed for key AIzaSyD-not-a-real-key-123456789":        "auth failed for key [redacted]",
		"openai: sk-proj-abcdefghijklmnopqrstuvwx rejected":           "openai: [redacted] rejected",
		"Authorization: Bearer abc.def-ghijklmnop":                    "Authorization: Bearer [redacted]",
		`{"api_key": "zzzzzzzz"}`:                                     `{"api_key": "[redacted]"}`,
		"url?token=abc123xyz&x=1":                                     "url?token=[redacted]&x=1",
		"remember from pat@example.com":                               "remember from [email]",
		"episode by Pat Example failed":                               "episode by [redacted] failed",
		"POST http://spark.tail1234.ts.net:8080/v1/chat timed out":    "POST [redacted]/chat timed out",
		`add_episode failed: "Todd prefers dark roast coffee always"`: "add_episode failed: [text removed]",
		"reading /Users/pat/.cmf/.env":                                "reading ~/.cmf/.env",
		"ghp_abcdefghijklmnopqrstuvwxyz0123 in log":                   "[redacted] in log",
	}
	for in, want := range cases {
		if got := r.Line(in); got != want {
			t.Errorf("Line(%q)\n got %q\nwant %q", in, got, want)
		}
	}
	if got := r.Line(strings.Repeat("x", 1000)); len(got) > MaxLine+10 {
		t.Errorf("not cut: %d", len(got))
	}
	// Shown settings' values stay readable.
	if got := r.Line("knowledge provider notion ready"); got != "knowledge provider notion ready" {
		t.Error(got)
	}
}

const logs = `cmf-server  | 2026-10-24 12:00:00 INFO server started
cmf-server  | 2026-10-24 12:00:01 WARNING slow embed
cmf-server  | 2026-10-24 12:00:02 ERROR recall failed: key AIzaSyD-not-a-real-key-123456789
cmf-server  | Traceback (most recent call last):
cmf-server  |   File "server/mcp.py", line 10, in recall
cmf-server  | redis.exceptions.ConnectionError: Error 111 connecting
cmf-poller  | 2026-10-24 12:00:03 CRITICAL poller stopped
cmf-falkordb  | 1:M 24 Oct 2026 12:00:00.000 * Ready to accept connections`

func TestErrorLines(t *testing.T) {
	got := redactor().ErrorLines(logs, 50)
	want := []string{
		"cmf-server  | 2026-10-24 12:00:02 ERROR recall failed: key [redacted]",
		"cmf-server  | Traceback (most recent call last):",
		"cmf-server  | redis.exceptions.ConnectionError: Error 111 connecting",
		"cmf-poller  | 2026-10-24 12:00:03 CRITICAL poller stopped",
	}
	if strings.Join(got, "\n") != strings.Join(want, "\n") {
		t.Fatalf("got\n%s", strings.Join(got, "\n"))
	}
	if last := redactor().ErrorLines(logs, 2); len(last) != 2 || !strings.Contains(last[1], "CRITICAL") {
		t.Fatal(last)
	}
}

func TestReportCarriesNoSecrets(t *testing.T) {
	h := &probe.Health{Status: "ok", Version: "0.9", Runtime: "docker", Graph: "mem-fabric-local",
		KnowledgeSources: []string{"notion"},
		BackgroundWrites: []probe.BackgroundWrite{{Status: "failed", ErrorKind: "provider_quota", FinishedAt: "2026-10-24T12:00:00Z"}},
		ConfigCheck:      &probe.ConfigCheck{Warnings: []string{"key AIzaSyD-not-a-real-key-123456789 looks odd"}},
		ClientsSeen:      []probe.ClientSeen{{Harness: "claude_desktop", LastSeen: time.Date(2026, 10, 24, 11, 0, 0, 0, time.UTC)}}}
	f := probe.Facts{OS: "darwin", Health: h, Nodes: 6769, Docker: probe.Docker{Installed: true, Responding: true, Version: "29.7.2"}}
	report := Build(Input{At: time.Date(2026, 10, 24, 13, 0, 0, 0, time.UTC), Version: "dev", Arch: "arm64", Facts: f,
		Results: []checks.Result{{Title: "Docker installed and running", Level: checks.OK, Detail: "Docker 29.7.2"}},
		Env:     env, Logs: logs, Redactor: redactor()})
	for k, v := range env {
		if v != "" && Hidden(k) && strings.Contains(report, v) {
			t.Errorf("%s's value is in the report", k)
		}
	}
	for _, want := range []string{"graph mem-fabric-local (6769 nodes", "COMPOSE_PROFILES=spark,capture", "GEMINI_API_KEY (set)",
		"EMPTY_ONE (empty)", "[ok] Docker installed and running", "last 4 error lines", "client seen: claude_desktop", "background write 2026-10-24T12:00:00Z failed provider_quota", "config warning: key [redacted] looks odd"} {
		if !strings.Contains(report, want) {
			t.Errorf("report lacks %q:\n%s", want, report)
		}
	}
}

func TestLineKeepsApostrophesAndURLs(t *testing.T) {
	r := redactor()
	cases := map[string]string{
		"couldn't read the logs: Docker isn't answering":     "couldn't read the logs: Docker isn't answering",
		`Get "http://127.0.0.1:8000/health": refused`:        `Get "http://127.0.0.1:8000/health": refused`,
		"query ('what does the user think about coffee') no": "query ([text removed]) no",
	}
	for in, want := range cases {
		if got := r.Line(in); got != want {
			t.Errorf("Line(%q)\n got %q\nwant %q", in, got, want)
		}
	}
}
