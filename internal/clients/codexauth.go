package clients

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/tmargolis/context-memory-fabric/internal/home"
)

// Codex keeps an OAuth sign-in per MCP server, apart from config.toml, and
// tries it before the entry's Authorization header. After `cmf clients set
// codex` moved Codex to the local address and token, a sign-in left from the
// public address was refused three times on every connect before the token
// got in (B21, 2026-10-26). Removing it was `codex mcp logout <name>`, a
// shell command users shouldn't need, so setting the token entry removes it.
//
// Codex stores sign-ins in the macOS Keychain (service codexKeychainService,
// account "<server name>|<hash>") or, where no keyring is used, in
// .credentials.json in the Codex home. The Keychain form was read on this
// Mac; the file form is matched the same way, by "<server name>|" keys.

const codexKeychainService = "Codex MCP Credentials"

// securityCmd runs macOS's `security` tool; tests replace it.
var securityCmd = func(args ...string) ([]byte, error) {
	return exec.Command("security", args...).Output()
}

var goos = runtime.GOOS

// userCodexHome is the signed-in user's own Codex home ($CODEX_HOME, else
// ~/.codex). The Keychain is the user's, not the config file's, so it is read
// and changed only for that Codex home: a test or another Env's config
// elsewhere must never remove the user's real sign-in (one did, 2026-10-26).
var userCodexHome = func() string {
	if c := os.Getenv("CODEX_HOME"); c != "" {
		return c
	}
	h, err := os.UserHomeDir()
	if err != nil {
		return ""
	}
	return filepath.Join(h, ".codex")
}

func sameDir(a, b string) bool {
	if a == "" || b == "" {
		return false
	}
	ra, errA := filepath.EvalSymlinks(a)
	rb, errB := filepath.EvalSymlinks(b)
	if errA != nil || errB != nil {
		return filepath.Clean(a) == filepath.Clean(b)
	}
	return ra == rb
}

// CodexLogin is one stored Codex sign-in for a CMF server name.
type CodexLogin struct {
	Store   string // "keychain" or the credentials file's path
	Account string // "<server name>|<hash>"
}

func codexLoginFor(account string, names []string) bool {
	for _, n := range names {
		if strings.HasPrefix(account, n+"|") {
			return true
		}
	}
	return false
}

func codexCredentialsFile(codexHome string) string {
	return filepath.Join(codexHome, ".credentials.json")
}

// CodexLogins lists the stored sign-ins for any of the server names. It reads
// only item attributes (never a secret), and errors leave the list short.
func CodexLogins(codexHome string, names []string) []CodexLogin {
	var out []CodexLogin
	if goos == "darwin" && sameDir(codexHome, userCodexHome()) {
		if dump, err := securityCmd("dump-keychain"); err == nil {
			for _, acct := range keychainAccounts(dump, codexKeychainService) {
				if codexLoginFor(acct, names) {
					out = append(out, CodexLogin{Store: "keychain", Account: acct})
				}
			}
		}
	}
	if data, err := os.ReadFile(codexCredentialsFile(codexHome)); err == nil {
		var creds map[string]json.RawMessage
		if json.Unmarshal(data, &creds) == nil {
			for acct := range creds {
				if codexLoginFor(acct, names) {
					out = append(out, CodexLogin{Store: codexCredentialsFile(codexHome), Account: acct})
				}
			}
		}
	}
	return out
}

// keychainAccounts reads the accounts of one service's items from
// `security dump-keychain` output (attributes only).
func keychainAccounts(dump []byte, service string) []string {
	var accts []string
	for _, item := range bytes.Split(dump, []byte("keychain: ")) {
		var acct, svce string
		for _, line := range strings.Split(string(item), "\n") {
			line = strings.TrimSpace(line)
			if v, ok := blobValue(line, `"acct"<blob>=`); ok {
				acct = v
			}
			if v, ok := blobValue(line, `"svce"<blob>=`); ok {
				svce = v
			}
		}
		if svce == service && acct != "" {
			accts = append(accts, acct)
		}
	}
	return accts
}

func blobValue(line, prefix string) (string, bool) {
	if !strings.HasPrefix(line, prefix) {
		return "", false
	}
	v := strings.TrimPrefix(line, prefix)
	if len(v) < 2 || v[0] != '"' || v[len(v)-1] != '"' {
		return "", false
	}
	return v[1 : len(v)-1], true
}

// forgetCodexLogins removes the sign-ins. A credentials file is backed up
// first; a Keychain item can't be copied without reading its secret, and
// signing in again recreates it, so it is removed without a copy.
func forgetCodexLogins(logins []CodexLogin, b *home.Backup) error {
	files := map[string][]string{}
	for _, l := range logins {
		if l.Store == "keychain" {
			if _, err := securityCmd("delete-generic-password", "-s", codexKeychainService, "-a", l.Account); err != nil {
				return fmt.Errorf("removing Codex's stored sign-in %q from the Keychain: %v", l.Account, err)
			}
			continue
		}
		files[l.Store] = append(files[l.Store], l.Account)
	}
	for path, accts := range files {
		data, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		var creds map[string]json.RawMessage
		if err := json.Unmarshal(data, &creds); err != nil {
			return fmt.Errorf("%s: %v", path, err)
		}
		for _, a := range accts {
			delete(creds, a)
		}
		out, _ := json.MarshalIndent(creds, "", "  ")
		if err := b.Save(path); err != nil {
			return fmt.Errorf("backing up %s: %w", path, err)
		}
		if err := home.WriteFile(path, out, 0o600); err != nil {
			return err
		}
	}
	return nil
}
