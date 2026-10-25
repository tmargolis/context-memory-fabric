// Package jsonedit edits one key of a JSON object while keeping every other
// key, in its order, as the file had it: apps own these files (Claude's
// settings, MCP configs) and a rewrite shouldn't reshuffle them.
package jsonedit

import (
	"bytes"
	"encoding/json"
	"fmt"
)

// Object is a JSON object that keeps its key order.
type Object struct {
	keys []string
	vals map[string]json.RawMessage
}

func New() *Object { return &Object{vals: map[string]json.RawMessage{}} }

// Parse reads an object; empty input is an empty object.
func Parse(data []byte) (*Object, error) {
	if len(bytes.TrimSpace(data)) == 0 {
		return New(), nil
	}
	dec := json.NewDecoder(bytes.NewReader(data))
	tok, err := dec.Token()
	if err != nil {
		return nil, err
	}
	if d, ok := tok.(json.Delim); !ok || d != '{' {
		return nil, fmt.Errorf("not a JSON object")
	}
	o := New()
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

func (o *Object) Keys() []string { return append([]string(nil), o.keys...) }

func (o *Object) Get(k string) (json.RawMessage, bool) { v, ok := o.vals[k]; return v, ok }

// Child parses the object under k (empty if absent).
func (o *Object) Child(k string) (*Object, error) {
	raw, ok := o.vals[k]
	if !ok {
		return New(), nil
	}
	return Parse(raw)
}

func (o *Object) Set(k string, v json.RawMessage) {
	if _, ok := o.vals[k]; !ok {
		o.keys = append(o.keys, k)
	}
	o.vals[k] = v
}

// SetValue marshals v under k.
func (o *Object) SetValue(k string, v any) error {
	raw, err := json.Marshal(v)
	if err != nil {
		return err
	}
	o.Set(k, raw)
	return nil
}

func (o *Object) Remove(k string) {
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

func (o *Object) Len() int { return len(o.keys) }

// Raw is the compact object.
func (o *Object) Raw() json.RawMessage {
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

// File is the object indented two spaces, with a final newline.
func (o *Object) File() ([]byte, error) {
	var out bytes.Buffer
	if err := json.Indent(&out, o.Raw(), "", "  "); err != nil {
		return nil, err
	}
	out.WriteByte('\n')
	return out.Bytes(), nil
}
