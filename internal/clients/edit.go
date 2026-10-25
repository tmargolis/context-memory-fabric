package clients

import (
	"bytes"
	"encoding/json"
	"fmt"
	"os"
	"regexp"
	"strconv"
	"strings"

	"github.com/tmargolis/context-memory-fabric/internal/home"
)

// object is a JSON object that keeps its key order, so rewriting one key of
// ~/.claude.json leaves the rest of the file as the app wrote it.
type object struct {
	keys []string
	vals map[string]json.RawMessage
}

func parseObject(data []byte) (*object, error) {
	dec := json.NewDecoder(bytes.NewReader(data))
	tok, err := dec.Token()
	if err != nil {
		return nil, err
	}
	if d, ok := tok.(json.Delim); !ok || d != '{' {
		return nil, fmt.Errorf("not a JSON object")
	}
	o := &object{vals: map[string]json.RawMessage{}}
	for dec.More() {
		tok, err := dec.Token()
		if err != nil {
			return nil, err
		}
		key := tok.(string)
		var raw json.RawMessage
		if err := dec.Decode(&raw); err != nil {
			return nil, err
		}
		if _, dup := o.vals[key]; !dup {
			o.keys = append(o.keys, key)
		}
		o.vals[key] = raw
	}
	if _, err := dec.Token(); err != nil {
		return nil, err
	}
	return o, nil
}

func (o *object) get(k string) (json.RawMessage, bool) { v, ok := o.vals[k]; return v, ok }

func (o *object) set(k string, v json.RawMessage) {
	if _, ok := o.vals[k]; !ok {
		o.keys = append(o.keys, k)
	}
	o.vals[k] = v
}

func (o *object) remove(k string) {
	if _, ok := o.vals[k]; !ok {
		return
	}
	delete(o.vals, k)
	for i, key := range o.keys {
		if key == k {
			o.keys = append(o.keys[:i], o.keys[i+1:]...)
			break
		}
	}
}

func (o *object) marshal() json.RawMessage {
	var b bytes.Buffer
	b.WriteByte('{')
	for i, k := range o.keys {
		if i > 0 {
			b.WriteByte(',')
		}
		name, _ := json.Marshal(k)
		b.Write(name)
		b.WriteByte(':')
		b.Write(o.vals[k])
	}
	b.WriteByte('}')
	return b.Bytes()
}

func entryFields(raw json.RawMessage) (map[string]string, []string) {
	var m map[string]any
	if json.Unmarshal(raw, &m) != nil {
		return nil, nil
	}
	fields := map[string]string{}
	for k, v := range m {
		if s, ok := v.(string); ok {
			fields[k] = s
		}
	}
	var args []string
	if list, ok := m["args"].([]any); ok {
		for _, a := range list {
			if s, ok := a.(string); ok {
				args = append(args, s)
			}
		}
	}
	return fields, args
}

// entry is the JSON entry an app gets for t.
func entry(s Style, t Target) json.RawMessage {
	u := t.LocalURL()
	var v map[string]any
	switch s {
	case Bridge:
		args := []any{"-y", "mcp-remote", u}
		v = map[string]any{"command": "npx", "args": args}
		if t.Token != "" {
			// mcp-remote's documented form; no space after the colon so the
			// header survives Windows argument splitting.
			v["args"] = append(args, "--header", "Authorization:Bearer ${CMF_MCP_AUTH_TOKEN}")
			v["env"] = map[string]string{"CMF_MCP_AUTH_TOKEN": t.Token}
		}
	case URLOnly:
		v = map[string]any{"url": u}
	default:
		v = map[string]any{"type": "http", "url": u}
	}
	if t.Token != "" && s != Bridge {
		v["headers"] = map[string]string{"Authorization": "Bearer " + t.Token}
	}
	raw, _ := json.Marshal(v)
	return raw
}

// Change is one planned rewrite of an app's config file.
type Change struct {
	App     App
	Path    string
	Old     []byte // nil: the file doesn't exist yet
	New     []byte
	Summary []string // one line per entry added, kept or removed
	Perm    os.FileMode
}

func (c *Change) Empty() bool { return bytes.Equal(c.Old, c.New) }

// Plan makes the change that leaves s with exactly one CMF entry reaching t
// (want) or none (!want). A correct entry that is already there, such as one
// pointing at the public URL, is kept rather than replaced.
func Plan(s State, t Target, want bool) (*Change, error) {
	if s.Err != "" {
		return nil, fmt.Errorf("%s: %s", s.Label, s.Err)
	}
	c := &Change{App: s.App, Path: s.Path, Perm: 0o644}
	if s.Exists {
		data, err := os.ReadFile(s.Path)
		if err != nil {
			return nil, err
		}
		c.Old = data
		if info, err := os.Stat(s.Path); err == nil {
			c.Perm = info.Mode().Perm()
		}
	}
	if t.Token != "" {
		c.Perm &^= 0o077 // the file will hold a token
	}
	keep := ""
	if want {
		for _, e := range s.CMF() {
			if e.Correct {
				keep = e.Name
				break
			}
		}
	}
	var remove []string
	for _, e := range s.CMF() {
		if e.Name != keep {
			remove = append(remove, e.Name)
			c.Summary = append(c.Summary, fmt.Sprintf("remove %q (%s)", e.Name, describe(e)))
		}
	}
	switch {
	case keep != "":
		c.Summary = append(c.Summary, fmt.Sprintf("keep %q (%s)", keep, s.entry(keep).URL))
	case want:
		c.Summary = append(c.Summary, fmt.Sprintf("add %q → %s", nameFor(s.App), t.LocalURL()))
	}
	var err error
	if s.App.Format == TOMLCodex {
		c.New = codexRewrite(string(c.Old), remove, want && keep == "", t)
	} else {
		c.New, err = jsonRewrite(s.App, c.Old, remove, want && keep == "", t)
	}
	if err != nil {
		return nil, err
	}
	if c.Empty() {
		c.Summary = nil
	}
	return c, nil
}

func (s State) entry(name string) Entry {
	for _, e := range s.Entries {
		if e.Name == name {
			return e
		}
	}
	return Entry{}
}

func describe(e Entry) string {
	switch {
	case e.Kind == "stdio":
		return "starts its own CMF server"
	case e.URL != "" && !e.Correct:
		return "points at " + e.URL + ", not this install"
	case e.URL != "":
		return "duplicate of " + e.URL
	}
	return e.Kind
}

func nameFor(a App) string {
	if a.Format == TOMLCodex {
		return CodexName
	}
	return Name
}

func jsonRewrite(a App, old []byte, remove []string, add bool, t Target) ([]byte, error) {
	root := &object{vals: map[string]json.RawMessage{}}
	if len(bytes.TrimSpace(old)) > 0 {
		var err error
		if root, err = parseObject(old); err != nil {
			return nil, err
		}
	}
	key := serversKey(a.Format)
	servers := &object{vals: map[string]json.RawMessage{}}
	if raw, ok := root.get(key); ok {
		var err error
		if servers, err = parseObject(raw); err != nil {
			return nil, err
		}
	}
	for _, name := range remove {
		servers.remove(name)
	}
	if add {
		servers.set(Name, entry(a.Style, t))
	}
	if len(remove) == 0 && !add {
		return old, nil
	}
	root.set(key, servers.marshal())
	var out bytes.Buffer
	if err := json.Indent(&out, root.marshal(), "", "  "); err != nil {
		return nil, err
	}
	out.WriteByte('\n')
	return out.Bytes(), nil
}

// --- Codex config.toml ------------------------------------------------------------

var tomlHeader = regexp.MustCompile(`^\s*\[([^\[\]]+)\]\s*(#.*)?$`)

// codexTable splits a table header name into its mcp_servers server name and
// whether it is that server's own table (not a subtable like .env).
func codexTable(header string) (name string, own bool, ok bool) {
	rest, found := strings.CutPrefix(strings.TrimSpace(header), "mcp_servers.")
	if !found {
		return "", false, false
	}
	if strings.HasPrefix(rest, `"`) {
		end := strings.Index(rest[1:], `"`)
		if end < 0 {
			return "", false, false
		}
		name, rest = rest[1:end+1], rest[end+2:] // rest: "" or ".env", ...
	} else {
		var sub bool
		if name, rest, sub = strings.Cut(rest, "."); sub {
			rest = "." + rest
		}
	}
	return strings.TrimSpace(name), strings.TrimSpace(rest) == "", true
}

var tomlString = regexp.MustCompile(`^\s*([A-Za-z0-9_-]+)\s*=\s*"((?:[^"\\]|\\.)*)"`)
var tomlArray = regexp.MustCompile(`^\s*args\s*=\s*\[(.*)\]`)

func codexEntries(text string, t Target) []Entry {
	var out []Entry
	var cur string
	fields := map[string]map[string]string{}
	args := map[string][]string{}
	order := []string{}
	for _, line := range strings.Split(text, "\n") {
		if m := tomlHeader.FindStringSubmatch(line); m != nil {
			name, own, ok := codexTable(m[1])
			cur = ""
			if ok && own {
				cur = name
				if _, seen := fields[name]; !seen {
					fields[name] = map[string]string{}
					order = append(order, name)
				}
			}
			continue
		}
		if cur == "" {
			continue
		}
		if m := tomlString.FindStringSubmatch(line); m != nil {
			fields[cur][m[1]], _ = strconv.Unquote(`"` + m[2] + `"`)
		}
		if m := tomlArray.FindStringSubmatch(line); m != nil {
			for _, s := range regexp.MustCompile(`"((?:[^"\\]|\\.)*)"`).FindAllStringSubmatch(m[1], -1) {
				args[cur] = append(args[cur], s[1])
			}
		}
	}
	for _, name := range order {
		out = append(out, classify(name, fields[name], args[name], t))
	}
	return out
}

// codexRewrite drops the named servers' tables (and their subtables) and,
// with add, appends CMF's own table.
func codexRewrite(text string, remove []string, add bool, t Target) []byte {
	drop := map[string]bool{}
	for _, n := range remove {
		drop[n] = true
	}
	var out []string
	skipping := false
	lines := strings.Split(text, "\n")
	for _, line := range lines {
		if m := tomlHeader.FindStringSubmatch(line); m != nil {
			name, _, ok := codexTable(m[1])
			skipping = ok && drop[name]
		}
		if !skipping {
			out = append(out, line)
		}
	}
	result := strings.Join(out, "\n")
	if add {
		result = strings.TrimRight(result, "\n")
		if result != "" {
			result += "\n\n"
		}
		result += "[mcp_servers." + CodexName + "]\nurl = " + strconv.Quote(t.LocalURL()) + "\n"
		if t.Token != "" {
			result += "http_headers = { Authorization = " + strconv.Quote("Bearer "+t.Token) + " }\n"
		}
	}
	if len(remove) == 0 && !add {
		return []byte(text)
	}
	if !strings.HasSuffix(result, "\n") {
		result += "\n"
	}
	return []byte(result)
}

// Write backs the file up into b, refuses if it changed since Plan read it,
// writes it, and reads it back to check the CMF entries are as planned.
func (c *Change) Write(b *home.Backup, t Target, env Env, want bool) error {
	if c.Empty() {
		return nil
	}
	now, err := os.ReadFile(c.Path)
	if err != nil && !os.IsNotExist(err) {
		return err
	}
	if !bytes.Equal(now, c.Old) {
		return fmt.Errorf("%s changed since cmf read it (is %s running and saving it?); run the command again", c.Path, c.App.Label)
	}
	if err := b.Save(c.Path); err != nil {
		return fmt.Errorf("backing up %s: %w", c.Path, err)
	}
	if err := home.WriteFile(c.Path, c.New, c.Perm); err != nil {
		return err
	}
	after := Inspect(c.App, env, t)
	switch cmf := after.CMF(); {
	case after.Err != "":
		return fmt.Errorf("%s doesn't read back: %s", c.Path, after.Err)
	case want && !after.Healthy():
		return fmt.Errorf("%s read back with %d CMF entries; expected one reaching this install", c.Path, len(cmf))
	case !want && len(cmf) > 0:
		return fmt.Errorf("%s still has a CMF entry after removing it", c.Path)
	}
	return nil
}
