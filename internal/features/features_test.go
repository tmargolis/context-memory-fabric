package features

import (
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/tmargolis/context-memory-fabric/internal/home"
)

// toddEnv is shaped like Todd's ~/.cmf/.env on 2026-10-24 (no real values).
const toddEnv = `LLM_WIKI_PATH=/Users/x/LLM_Wiki
CMF_LLM_PROVIDER=local
CMF_EMBED_PROVIDER=local
CMF_LOCAL_BASE_URL=http://127.0.0.1:12345/v1
EMBEDDING_DIM=768
CMF_CAPTURE_LLM_PROVIDER=local
COMPOSE_PROFILES=spark
CMF_KNOWLEDGE_PROVIDERS=notion
CMF_NOTION_TOKEN=ntn_abcdefghijklmnop
CMF_NOTION_WORKSPACE=ws
COMPOSE_PROFILES=spark,capture
`

func env(t *testing.T, text string) *home.Env {
	path := filepath.Join(t.TempDir(), ".env")
	os.WriteFile(path, []byte(text), 0o600)
	e, err := home.ReadEnv(path)
	if err != nil {
		t.Fatal(err)
	}
	return e
}

func manifest(t *testing.T) *Manifest {
	m, err := Load()
	if err != nil {
		t.Fatal(err)
	}
	return m
}

func request(t *testing.T, js string) *Request {
	r, err := ParseRequest([]byte(js))
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func plan(t *testing.T, current, js string, ctx Context) *Plan {
	return manifest(t).Plan(env(t, current), request(t, js), ctx)
}

func hasError(p *Plan, substr string) bool {
	return slices.ContainsFunc(p.Errors, func(e string) bool { return strings.Contains(e, substr) })
}

func TestStatesOfToddsInstall(t *testing.T) {
	m := manifest(t)
	e := env(t, toddEnv)
	on := map[string]bool{}
	for _, s := range m.States(e) {
		on[s.ID] = s.Enabled
		if s.ID == "notion" && s.Inputs[0].Value != "••••mnop" {
			t.Fatalf("secret must be masked: %q", s.Inputs[0].Value)
		}
	}
	want := map[string]bool{"core": true, "llm": true, "notion": true, "own-knowledge": true, "capture": true,
		"wiki": false, "demo-wiki": false, "memory-only": false}
	for id, w := range want {
		if on[id] != w {
			t.Errorf("%s: got %v want %v", id, on[id], w)
		}
	}
	llm := m.Feature("llm").Input("providers")
	if got := Values(e, llm); !slices.Equal(got, []string{"local"}) {
		t.Fatalf("ranked list falls back to CMF_LLM_PROVIDER: %v", got)
	}
}

func TestFreshInstallDefaults(t *testing.T) {
	m := manifest(t)
	e := env(t, "")
	if !m.Enabled(e, "memory-only") || m.Enabled(e, "wiki") || m.Enabled(e, "capture") {
		t.Fatal("no .env: memory only, no capture")
	}
	if Value(e, m.Feature("embeddings").Input("provider")) != "gemini" {
		t.Fatal("server default embeddings provider")
	}
}

func TestTurnOffCaptureStopsThePoller(t *testing.T) {
	p := plan(t, toddEnv, `{"features": {"capture": {"enabled": false}}}`, Context{Nodes: 10})
	if !p.OK() {
		t.Fatal(p.Errors)
	}
	if got := p.Env.Get("COMPOSE_PROFILES"); got != "spark" {
		t.Fatalf("other profiles kept: %q", got)
	}
	if !slices.Equal(p.Stop, []string{"cmf-poller"}) || !slices.Equal(p.Disabled, []string{"capture"}) {
		t.Fatalf("%+v", p)
	}
	if len(p.Changes) != 1 || p.Changes[0].String() != "set COMPOSE_PROFILES=spark (was spark,capture)" {
		t.Fatalf("%+v", p.Changes)
	}
}

func TestAddWikiNextToNotion(t *testing.T) {
	dir := t.TempDir()
	p := plan(t, toddEnv, `{"features": {"wiki": {"enabled": true, "path": "`+dir+`"}}}`, Context{Nodes: 10})
	if !p.OK() {
		t.Fatal(p.Errors)
	}
	if got := p.Env.Get("CMF_KNOWLEDGE_PROVIDERS"); got != "notion,wiki" {
		t.Fatal(got)
	}
	if p.Env.Get("LLM_WIKI_PATH") != dir || !slices.Equal(p.Enabled, []string{"wiki"}) {
		t.Fatalf("%+v", p)
	}
}

func TestWikiPathMustExist(t *testing.T) {
	p := plan(t, toddEnv, `{"features": {"wiki": {"enabled": true, "path": "/no/such/folder"}}}`, Context{})
	if !hasError(p, "isn't a folder") {
		t.Fatal(p.Errors)
	}
	p = plan(t, toddEnv, `{"features": {"wiki": {"enabled": true}}}`, Context{})
	if !p.OK() || p.Env.Get("CMF_KNOWLEDGE_PROVIDERS") != "notion,wiki" {
		t.Fatal("wiki with LLM_WIKI_PATH already set is fine", p.Errors)
	}
}

func TestKnowledgeDefaultListIsKept(t *testing.T) {
	// CMF_KNOWLEDGE_PROVIDERS unset means "wiki"; adding Notion keeps it.
	dir := t.TempDir()
	p := plan(t, "LLM_WIKI_PATH="+dir+"\nGEMINI_API_KEY=g-123456789\n", `{"features": {"notion": {"enabled": true, "token": "ntn_x"}}}`, Context{})
	if !p.OK() || p.Env.Get("CMF_KNOWLEDGE_PROVIDERS") != "wiki,notion" {
		t.Fatalf("%v %q", p.Errors, p.Env.Get("CMF_KNOWLEDGE_PROVIDERS"))
	}
	if slices.ContainsFunc(p.Changes, func(c Change) bool { return c.Key == "CMF_NOTION_TOKEN" && strings.Contains(c.String(), "ntn_x") }) {
		t.Fatal("secrets are masked in the plan")
	}
}

func TestNotionNeedsAToken(t *testing.T) {
	p := plan(t, "", `{"features": {"notion": {"enabled": true}}}`, Context{})
	if !hasError(p, "Notion needs Integration token") {
		t.Fatal(p.Errors)
	}
}

func TestAnthropicEmbeddingsAreForbidden(t *testing.T) {
	p := plan(t, toddEnv, `{"features": {"embeddings": {"provider": "anthropic"}}}`, Context{Nodes: 0})
	if !hasError(p, "Anthropic has no embeddings API") {
		t.Fatal(p.Errors)
	}
}

func TestEmbeddingsLockedWhenGraphHasData(t *testing.T) {
	e := toddEnv + "OPENAI_API_KEY=sk-123456789\n"
	if p := plan(t, e, `{"features": {"embeddings": {"provider": "openai"}}}`, Context{Nodes: 6769}); !hasError(p, "the graph has 6769 nodes") {
		t.Fatal(p.Errors)
	}
	if p := plan(t, e, `{"features": {"embeddings": {"provider": "openai"}}}`, Context{Nodes: -1}); !hasError(p, "can't confirm the graph is empty") {
		t.Fatal(p.Errors)
	}
	if p := plan(t, e, `{"features": {"embeddings": {"provider": "openai"}}}`, Context{Nodes: 0}); !p.OK() {
		t.Fatal(p.Errors)
	}
	if p := plan(t, e, `{"features": {"embeddings": {"provider": "local"}}}`, Context{Nodes: 6769}); !p.OK() || !p.Empty() {
		t.Fatal("same value is not a change", p.Errors)
	}
}

// One LLM card since v0.1.2: saving it takes over an older install's separate
// background-capture provider, and the review says so.
func TestLLMCardReplacesSeparateCaptureProvider(t *testing.T) {
	p := plan(t, toddEnv, `{"features": {"llm": {"providers": ["local", "gemini"]}, "provider-keys": {"gemini_api_key": "AIza123456789"}}}`, Context{})
	if !p.OK() {
		t.Fatal(p.Errors)
	}
	if p.Env.Get("CMF_CAPTURE_LLM_PROVIDER") != "" || !slices.ContainsFunc(p.Changes, func(c Change) bool {
		return c.Key == "CMF_CAPTURE_LLM_PROVIDER" && c.Removed
	}) {
		t.Fatal("the separate capture setting must be removed", p.Changes)
	}
	if !slices.ContainsFunc(p.Notes, func(n string) bool { return strings.Contains(n, "it was set separately to local") }) {
		t.Fatal(p.Notes)
	}
	if manifest(t).Feature("capture-llm") != nil {
		t.Fatal("no separate capture card")
	}
	// An unrelated change leaves the older setting alone.
	if p := plan(t, toddEnv, `{"features": {"capture": {"enabled": false}}}`, Context{}); p.Env.Get("CMF_CAPTURE_LLM_PROVIDER") != "local" {
		t.Fatal("touched without saving the LLM card")
	}
}

func TestRankedProvidersNeedKeysAndWriteBothVariables(t *testing.T) {
	p := plan(t, toddEnv, `{"features": {"llm": {"providers": ["anthropic", "local"]}}}`, Context{})
	if !hasError(p, "uses anthropic, but ANTHROPIC_API_KEY isn't set") {
		t.Fatal(p.Errors)
	}
	p = plan(t, toddEnv, `{"features": {"llm": {"providers": ["anthropic", "local"]}, "provider-keys": {"anthropic_api_key": "sk-ant-123456789"}}}`, Context{})
	if !p.OK() {
		t.Fatal(p.Errors)
	}
	if p.Env.Get("CMF_LLM_PROVIDERS") != "anthropic,local" || p.Env.Get("CMF_LLM_PROVIDER") != "anthropic" {
		t.Fatal("ranked list plus its first item")
	}
	if p := plan(t, toddEnv, `{"features": {"llm": {"providers": "local,local"}}}`, Context{}); !hasError(p, "listed twice") {
		t.Fatal(p.Errors)
	}
	if p := plan(t, toddEnv, `{"features": {"llm": {"providers": ["bard"]}}}`, Context{}); !hasError(p, `"bard" isn't one of`) {
		t.Fatal(p.Errors)
	}
}

func TestPlaceholderKeyDoesNotCount(t *testing.T) {
	p := plan(t, "OPENAI_API_KEY=your_openai_key_here\n", `{"features": {"llm": {"providers": ["openai"]}}}`, Context{})
	if !hasError(p, "OPENAI_API_KEY isn't set") {
		t.Fatal(p.Errors)
	}
}

func TestDemoWikiAndOwnSourcesAreExclusive(t *testing.T) {
	p := plan(t, toddEnv, `{"features": {"demo-wiki": {"enabled": true}}}`, Context{Repo: "/src/cmf"})
	if !hasError(p, "Pick one knowledge option") {
		t.Fatal(p.Errors)
	}
	p = plan(t, toddEnv, `{"features": {"demo-wiki": {"enabled": true}, "notion": {"enabled": false}}}`, Context{Repo: "/src/cmf"})
	if !p.OK() || p.Env.Get("LLM_WIKI_PATH") != "/src/cmf/starter-wiki" || p.Env.Get("CMF_KNOWLEDGE_PROVIDERS") != "wiki" || p.Env.Has("CMF_NOTION_TOKEN") {
		t.Fatalf("%v %q %q", p.Errors, p.Env.Get("LLM_WIKI_PATH"), p.Env.Get("CMF_KNOWLEDGE_PROVIDERS"))
	}
	if p.StarterWiki != "" {
		t.Fatalf("Developer mode uses the checkout's wiki, got %q to write", p.StarterWiki)
	}
	// Standard mode: the binary writes its copy into the CMF home.
	p = plan(t, toddEnv, `{"features": {"demo-wiki": {"enabled": true}, "notion": {"enabled": false}}}`, Context{Home: "/h/.cmf"})
	if !p.OK() || p.Env.Get("LLM_WIKI_PATH") != "/h/.cmf/starter-wiki" || p.StarterWiki != "/h/.cmf/starter-wiki" || !manifest(t).Enabled(p.Env, "demo-wiki") {
		t.Fatalf("%v %q %q", p.Errors, p.Env.Get("LLM_WIKI_PATH"), p.StarterWiki)
	}
	p = plan(t, toddEnv, `{"features": {"demo-wiki": {"enabled": true}, "notion": {"enabled": false}}}`, Context{})
	if !hasError(p, "can't tell where CMF is installed") {
		t.Fatal(p.Errors)
	}
}

func TestMemoryOnlyByTurningSourcesOff(t *testing.T) {
	p := plan(t, toddEnv, `{"features": {"notion": {"enabled": false}}}`, Context{})
	if !p.OK() || !manifest(t).Enabled(p.Env, "memory-only") {
		t.Fatal(p.Errors)
	}
}

func TestRequestErrorsAreAllReported(t *testing.T) {
	p := plan(t, toddEnv, `{"features": {"memory-only": {"enabled": true}, "core": {"enabled": false}, "teleport": {}, "wiki": {"path": "/tmp"}, "notion": {"colour": "red"}}}`, Context{})
	for _, want := range []string{"memory-only can't be set directly", "always on", `unknown feature "teleport"`, "add \"enabled\": true", `no input "colour"`} {
		if !hasError(p, want) {
			t.Errorf("missing %q in %v", want, p.Errors)
		}
	}
}

func TestExistingProblemsAreWarningsNotBlockers(t *testing.T) {
	// GEMINI is the default LLM and has no key: already broken before.
	p := plan(t, "", `{"features": {"capture": {"enabled": true}}}`, Context{})
	if !p.OK() || len(p.Warnings) == 0 {
		t.Fatalf("errors %v warnings %v", p.Errors, p.Warnings)
	}
}

func TestStopsBetweenEnvs(t *testing.T) {
	m := manifest(t)
	if got := m.Stops(env(t, toddEnv), env(t, "COMPOSE_PROFILES=spark\n")); !slices.Equal(got, []string{"cmf-poller"}) {
		t.Fatal(got)
	}
}

func TestParseRequestRejectsUnknownTopLevel(t *testing.T) {
	if _, err := ParseRequest([]byte(`{"feature": {}}`)); err == nil {
		t.Fatal("typo at the top level")
	}
	if _, err := ParseRequest([]byte(`{"features": {}}`)); err == nil {
		t.Fatal("empty request")
	}
}

func TestManifestChecksItself(t *testing.T) {
	bad := `{"groups": ["core"], "features": [{"id": "a", "group": "core", "enabled_when": {"feature": "a"}, "on_enable": {}, "on_disable": {}}]}`
	if _, err := Parse([]byte(bad)); err == nil || !strings.Contains(err.Error(), "refers back to itself") {
		t.Fatal(err)
	}
	bad = `{"groups": ["core"], "features": [{"id": "a", "group": "core", "always_on": true}], "rules": [{"type": "conflicts", "feature": "a", "with": "b"}]}`
	if _, err := Parse([]byte(bad)); err == nil || !strings.Contains(err.Error(), `unknown feature "b"`) {
		t.Fatal(err)
	}
}
