package mode

import (
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/tmargolis/context-memory-fabric/deploy/docker"
	"github.com/tmargolis/context-memory-fabric/internal/home"
)

const consolidatedVolumes = `# Added by scripts/consolidate_install.py: the pre-consolidation FalkorDB volume,
# attached as is (no data copy).
volumes:
  falkordb_data:
    external: true
    name: context-memory-fabric_falkordb_data
`

func fakeRepo(t *testing.T) string {
	repo := t.TempDir()
	os.MkdirAll(filepath.Join(repo, "server"), 0o755)
	os.WriteFile(filepath.Join(repo, "Dockerfile"), nil, 0o644)
	os.WriteFile(filepath.Join(repo, "server", "mcp.py"), nil, 0o644)
	return repo
}

func newHome(t *testing.T) home.Home {
	h := home.Home{Dir: t.TempDir()}
	os.WriteFile(h.EnvFile(), []byte("OPENAI_API_KEY=sk-1\n"), 0o600)
	return h
}

func TestDetectStandardWithoutOverride(t *testing.T) {
	h := newHome(t)
	state, err := Load(h)
	if err != nil {
		t.Fatal(err)
	}
	if state.Mode.Mode != Standard || state.Source != "detected" || !state.Consistent() {
		t.Fatalf("%+v", state)
	}
}

func TestDetectDeveloperFromConsolidatedInstall(t *testing.T) {
	h := newHome(t)
	os.WriteFile(h.OverrideFile(), append(append([]byte{}, docker.ComposeDev...), []byte("\n"+consolidatedVolumes)...), 0o644)
	os.WriteFile(h.EnvFile(), []byte("CMF_REPO=/src/cmf\n"), 0o600)
	state, _ := Load(h)
	if state.Mode != (Mode{Mode: Developer, Repo: "/src/cmf"}) {
		t.Fatalf("%+v", state)
	}
}

func TestModeJSONMismatchIsReported(t *testing.T) {
	h := newHome(t)
	os.WriteFile(h.ModeFile(), []byte(`{"mode":"developer","repo":"/src/cmf"}`), 0o644)
	state, err := Load(h)
	if err != nil {
		t.Fatal(err)
	}
	if state.Source != "mode.json" || state.Consistent() {
		t.Fatalf("%+v", state)
	}
	os.WriteFile(h.ModeFile(), []byte(`{"mode":"turbo"}`), 0o644)
	if _, err := Load(h); err == nil {
		t.Fatal("unknown mode must fail")
	}
}

func TestSwitchToDeveloperAndBack(t *testing.T) {
	h := newHome(t)
	os.WriteFile(h.OverrideFile(), []byte("# standard\n\n"+consolidatedVolumes), 0o644)
	os.WriteFile(h.ComposeFile(), docker.Compose, 0o644) // current: no compose change
	repo := fakeRepo(t)

	changes, err := Plan(h, Mode{Mode: Developer, Repo: repo})
	if err != nil {
		t.Fatal(err)
	}
	if len(changes) != 3 {
		t.Fatalf("mode.json, override, .env; got %d", len(changes))
	}
	backup, err := Apply(h, changes, time.Date(2026, 10, 24, 10, 0, 0, 0, time.UTC))
	if err != nil {
		t.Fatal(err)
	}
	override, _ := os.ReadFile(h.OverrideFile())
	if !strings.HasPrefix(string(override), string(docker.ComposeDev[:40])) || !strings.Contains(string(override), "name: context-memory-fabric_falkordb_data") {
		t.Fatalf("developer override must be the overlay plus the install's volumes:\n%s", override)
	}
	if env, _ := home.ReadEnv(h.EnvFile()); env.Get("CMF_REPO") != repo || env.Get("OPENAI_API_KEY") != "sk-1" {
		t.Fatal("CMF_REPO set, other keys kept")
	}
	if data, _ := os.ReadFile(filepath.Join(backup.Dir(), "home", "compose.override.yml")); !strings.Contains(string(data), "# standard") {
		t.Fatal("old override backed up")
	}
	if state, _ := Load(h); !state.Consistent() || state.Mode.Repo != repo {
		t.Fatalf("%+v", state)
	}
	if again, _ := Plan(h, Mode{Mode: Developer, Repo: repo}); len(again) != 0 {
		t.Fatalf("second plan should be empty, got %+v", again)
	}

	changes, err = Plan(h, Mode{Mode: Standard})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := Apply(h, changes, time.Date(2026, 10, 24, 10, 1, 0, 0, time.UTC)); err != nil {
		t.Fatal(err)
	}
	override, _ = os.ReadFile(h.OverrideFile())
	if strings.Contains(string(override), "build:") || !strings.Contains(string(override), "external: true") {
		t.Fatalf("standard override keeps only the volumes:\n%s", override)
	}
	if state, _ := Load(h); state.Mode.Mode != Standard || !state.Consistent() {
		t.Fatalf("%+v", state)
	}
}

func TestStandardWithoutLocalVolumesRemovesOverride(t *testing.T) {
	h := newHome(t)
	os.WriteFile(h.OverrideFile(), docker.ComposeDev, 0o644)
	changes, _ := Plan(h, Mode{Mode: Standard})
	var removal bool
	for _, c := range changes {
		removal = removal || (c.Path == h.OverrideFile() && c.Data == nil)
	}
	if !removal {
		t.Fatalf("%+v", changes)
	}
	backup, err := Apply(h, changes, time.Now())
	if err != nil {
		t.Fatal(err)
	}
	if home.Exists(h.OverrideFile()) || !home.Exists(filepath.Join(backup.Dir(), "home", "compose.override.yml")) {
		t.Fatal("override removed, copy kept in the backup")
	}
}

func TestDeveloperNeedsACheckout(t *testing.T) {
	h := newHome(t)
	if _, err := Plan(h, Mode{Mode: Developer}); err == nil {
		t.Fatal("no repo")
	}
	if _, err := Plan(h, Mode{Mode: Developer, Repo: t.TempDir()}); err == nil {
		t.Fatal("not a checkout")
	}
	if _, err := Plan(h, Mode{Mode: Developer, Repo: "relative/path"}); err == nil {
		t.Fatal("relative path")
	}
}

func TestLocalVolumes(t *testing.T) {
	if LocalVolumes("services:\n  a: {}\n") != "" {
		t.Fatal("no volumes section")
	}
	got := LocalVolumes("services:\n  a: {}\n\n" + consolidatedVolumes + "networks:\n  x: {}\n")
	if got != consolidatedVolumes {
		t.Fatalf("got\n%s", got)
	}
}

func TestPinImage(t *testing.T) {
	h := home.Home{Dir: t.TempDir()}
	if c, _ := PinImage(h, nil, "dev"); len(c) != 0 {
		t.Fatal("a dev build pins nothing")
	}
	if c, _ := PinImage(h, nil, "0.1.4-dev"); len(c) != 0 {
		t.Fatal("a dev build pins nothing")
	}
	c, err := PinImage(h, nil, "v0.2.0")
	if err != nil || len(c) != 1 || !strings.Contains(string(c[0].Data), "CMF_VERSION=0.2.0") {
		t.Fatalf("%v %+v", err, c)
	}
	os.WriteFile(h.EnvFile(), []byte("CMF_VERSION=0.2.0\n"), 0o600)
	if c, _ := PinImage(h, nil, "0.2.0"); len(c) != 0 {
		t.Fatal("already pinned")
	}
}

func TestPlanWritesOrUpdatesComposeFile(t *testing.T) {
	h := newHome(t)
	c, _ := Plan(h, Mode{Mode: Standard})
	if len(c) == 0 || c[0].Path != h.ComposeFile() && !strings.Contains(fmt.Sprint(c), "compose.yml") {
		t.Fatalf("%+v", c)
	}
	os.WriteFile(h.ComposeFile(), []byte("old"), 0o644)
	c, _ = Plan(h, Mode{Mode: Standard})
	found := false
	for _, ch := range c {
		found = found || (ch.Path == h.ComposeFile() && strings.Contains(ch.Note, "update"))
	}
	if !found {
		t.Fatalf("%+v", c)
	}
}
