package cli

import (
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"testing"
)

func TestPullRestoresAMissingFile(t *testing.T) {
	env := readEnv(t)
	path := filepath.Join(env.WorkDir, "notes", "missing.md")
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		t.Fatal(err)
	}
	origin := fmt.Sprintf("@file(dev:%s)", filepath.ToSlash(path))
	env.Daemon.Handle("GET", "/v1/notes", 200, map[string]any{
		"notes": []any{map[string]any{
			"id": "notes/missing", "kind": "note",
			"origin": map[string]any{"device": "d1", "pointer": origin},
		}},
	})
	env.Daemon.HandleFunc("GET", "/raw/demo/notes/missing", func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("committed\n"))
	})

	env.MustMem("pull")
	if data, err := os.ReadFile(path); err != nil || string(data) != "committed\n" {
		t.Fatalf("pulled bytes = %q, err=%v", data, err)
	}
}

func TestExportWritesIntoACleanDirectory(t *testing.T) {
	env := readEnv(t)
	env.Daemon.Handle("GET", "/v1/notes", 200, map[string]any{
		"notes": []any{map[string]any{"id": "notes/a", "kind": "note"}},
	})
	env.Daemon.HandleFunc("GET", "/raw/demo/notes/a", func(w http.ResponseWriter, r *http.Request) {
		_, _ = w.Write([]byte("body\n"))
	})
	destination := t.TempDir()

	env.MustMem("export", "demo", "--dst", destination)
	path := filepath.Join(destination, "notes", "a.md")
	if data, err := os.ReadFile(path); err != nil || string(data) != "body\n" {
		t.Fatalf("exported bytes = %q, err=%v", data, err)
	}
}
