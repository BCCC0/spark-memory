package cli

import (
	"flag"
	"os"
	"path/filepath"
	"testing"

	"github.com/BCCC0/spark-memory/client/internal/testenv"
)

var updateGolden = flag.Bool("update", false, "update golden files")

func seedHumanRoutes(env *testenv.Env) {
	d := env.Daemon
	d.Handle("GET", "/v1/notes", 200, map[string]any{"notes": []any{
		map[string]any{"id": "a", "status": "active", "type": "reference", "summary": "s"},
	}})
	d.Handle("GET", "/v1/notes/ops/boot", 200, map[string]any{
		"id": "ops/boot", "status": "committed", "kind": "note", "body": "hello", "bytes": 5,
	})
	d.Handle("GET", "/v1/manifest", 200, map[string]any{"revision": "abc", "entries": []any{map[string]any{"id": "a"}}})
	d.Handle("GET", "/v1/search", 200, map[string]any{"mode": "text", "hits": []any{
		map[string]any{"id": "a", "summary": "s", "line_range": []any{1, 2}, "matched_by": []any{"text"}},
	}})
	d.Handle("GET", "/v1/projects", 200, []any{map[string]any{"project": "demo", "notes": 1, "updated": "now"}})
	d.Handle("GET", "/v1/index/status", 200, map[string]any{
		"kind": "text", "head": "abc", "text": map[string]any{"indexed_head": "abc", "age": "0s"},
		"hybrid": map[string]any{"available": false},
	})
	d.Handle("GET", "/v1/devices", 200, []any{map[string]any{"device_id": "d1", "display_name": "test-device"}})
	d.Handle("GET", "/v1/queue", 200, map[string]any{"through": 1, "pending": 0, "indexed_head": "abc", "head": "abc", "failed": []any{}})
}

func TestHumanGolden(t *testing.T) {
	env := readEnv(t)
	seedHumanRoutes(env)
	cases := []struct {
		name string
		args []string
	}{
		{"ls", []string{"ls"}},
		{"read", []string{"read", "ops/boot"}},
		{"manifest", []string{"manifest"}},
		{"search", []string{"search", "hello"}},
		{"projects", []string{"projects"}},
		{"index", []string{"index"}},
		{"devices", []string{"devices"}},
		{"flush", []string{"flush"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			result := env.MustMem(append(tc.args, "--human")...)
			path := filepath.Join("..", "..", "testdata", "golden", "human_"+tc.name+".txt")
			if *updateGolden {
				if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(path, []byte(result.Stdout), 0644); err != nil {
					t.Fatal(err)
				}
				return
			}
			want, err := os.ReadFile(path)
			if err != nil {
				t.Fatalf("read golden: %v (run go test ./internal/cli -update)", err)
			}
			if result.Stdout != string(want) {
				t.Fatalf("human output differs\n--- got ---\n%s\n--- want ---\n%s", result.Stdout, want)
			}
		})
	}
}
