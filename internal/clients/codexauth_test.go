package clients

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// No test may reach the real Keychain: removing a sign-in there is for real.
func TestMain(m *testing.M) {
	securityCmd = func(args ...string) ([]byte, error) { return nil, errors.New("no keychain in tests") }
	userCodexHome = func() string { return "" }
	os.Exit(m.Run())
}

const dump = `keychain: "/Users/u/Library/Keychains/login.keychain-db"
version: 512
class: "genp"
attributes:
    "acct"<blob>="context_memory_fabric|c6076efd6dd8d0e5"
    "svce"<blob>="Codex MCP Credentials"
keychain: "/Users/u/Library/Keychains/login.keychain-db"
class: "genp"
attributes:
    "acct"<blob>="other_server|0123"
    "svce"<blob>="Codex MCP Credentials"
keychain: "/Users/u/Library/Keychains/login.keychain-db"
class: "genp"
attributes:
    "acct"<blob>="context_memory_fabric|ffff"
    "svce"<blob>="Something Else"
`

// fakeKeychain answers dump-keychain with `dump` and records deletions.
func fakeKeychain(t *testing.T, codexHome string) *[]string {
	var deleted []string
	prevCmd, prevOS, prevHome := securityCmd, goos, userCodexHome
	userCodexHome = func() string { return codexHome }
	securityCmd = func(args ...string) ([]byte, error) {
		switch args[0] {
		case "dump-keychain":
			return []byte(dump), nil
		case "delete-generic-password":
			deleted = append(deleted, strings.Join(args[1:], " "))
			return nil, nil
		}
		return nil, errors.New("unexpected")
	}
	goos = "darwin"
	t.Cleanup(func() { securityCmd, goos, userCodexHome = prevCmd, prevOS, prevHome })
	return &deleted
}

func TestKeychainAccounts(t *testing.T) {
	got := keychainAccounts([]byte(dump), codexKeychainService)
	if strings.Join(got, ",") != "context_memory_fabric|c6076efd6dd8d0e5,other_server|0123" {
		t.Fatal(got)
	}
}

func TestCodexSetRemovesStaleSignIn(t *testing.T) {
	env, b := setup(t)
	a := app("codex")
	write(t, a.Path(env), codex)
	deleted := fakeKeychain(t, filepath.Dir(a.Path(env)))
	c, err := Plan(Inspect(a, env, target), target, true)
	if err != nil {
		t.Fatal(err)
	}
	if len(c.ForgetLogins) != 1 || !strings.Contains(strings.Join(c.Summary, "\n"), `remove Codex's old sign-in for "context_memory_fabric"`) {
		t.Fatalf("%+v", c.Summary)
	}
	if err := c.Write(b, target, env, true); err != nil {
		t.Fatal(err)
	}
	if strings.Join(*deleted, ";") != "-s Codex MCP Credentials -a context_memory_fabric|c6076efd6dd8d0e5" {
		t.Fatalf("deleted %q; other servers' sign-ins must stay", *deleted)
	}

	// Already on the token: the config is unchanged, but the sign-in still goes.
	*deleted = nil
	c, err = Plan(Inspect(a, env, target), target, true)
	if err != nil {
		t.Fatal(err)
	}
	if c.Empty() || len(c.ForgetLogins) != 1 {
		t.Fatalf("%+v", c)
	}
	if err := c.Write(b, target, env, true); err != nil || len(*deleted) != 1 {
		t.Fatal(err, *deleted)
	}

	// No local token (OAuth is how it signs in): the sign-in is kept.
	if c, _ := Plan(Inspect(a, env, oauthOnly), oauthOnly, true); len(c.ForgetLogins) != 0 {
		t.Fatalf("%+v", c.ForgetLogins)
	}

	// Another Codex home than the user's own: the Keychain is left alone.
	userCodexHome = func() string { return "/elsewhere/.codex" }
	if c, _ := Plan(Inspect(a, env, target), target, true); len(c.ForgetLogins) != 0 {
		t.Fatalf("%+v", c.ForgetLogins)
	}
}

func TestCodexCredentialsFileSignIn(t *testing.T) {
	env, b := setup(t)
	a := app("codex")
	write(t, a.Path(env), codex)
	creds := filepath.Join(filepath.Dir(a.Path(env)), ".credentials.json")
	write(t, creds, `{"context_memory_fabric|abc": {"token": "x"}, "other|1": {"token": "y"}}`)
	c, err := Plan(Inspect(a, env, target), target, true)
	if err != nil || len(c.ForgetLogins) != 1 {
		t.Fatal(err, c.ForgetLogins)
	}
	if err := c.Write(b, target, env, true); err != nil {
		t.Fatal(err)
	}
	got, _ := os.ReadFile(creds)
	if strings.Contains(string(got), "context_memory_fabric") || !strings.Contains(string(got), `"other|1"`) {
		t.Fatal(string(got))
	}
}
