package cli

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestPathMoveUsesRepathAndRenamesLocally(t *testing.T) {
	env := readEnv(t)
	d := env.Daemon
	d.Handle("GET", "/v1/status", 200, map[string]any{"head": "abc"})
	d.Handle("GET", "/v1/queue", 200, map[string]any{
		"through": 1, "pending": 0, "failed": []any{},
	})
	d.HandleFunc("POST", "/v1/repath", func(w http.ResponseWriter, r *http.Request) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		w.Header().Set("Content-Type", "application/json")
		if body["dry_run"] == true {
			_, _ = w.Write([]byte(`{"id":"old","sha256":"sha","dry_run":true}`))
			return
		}
		_, _ = w.Write([]byte(`{"status":"queued","receipt":"j_1","seq":1}`))
	})
	env.Write("old.md", "---\nsummary: old\n---\nbody\n")

	result := env.MustMem("mv", "old.md", "new.md")
	if result.JSON["status"] != "committed" {
		t.Fatalf("result: %v", result.JSON)
	}
	if _, err := os.Stat(filepath.Join(env.WorkDir, "new.md")); err != nil {
		t.Fatal("new.md was not created")
	}
	if _, err := os.Stat(filepath.Join(env.WorkDir, "old.md")); !os.IsNotExist(err) {
		t.Fatal("old.md still exists")
	}
}

func TestOldIDMoveGetsAnExplicitHint(t *testing.T) {
	env := readEnv(t)
	result := env.Mem("mv", "ops/old", "ops/new")
	if result.Exit != 64 {
		t.Fatalf("exit = %d, stderr=%s", result.Exit, result.Stderr)
	}
	if !strings.Contains(result.Stderr, "--id") {
		t.Fatalf("stderr = %s", result.Stderr)
	}
}

func TestPathMoveRequiresAnExistingSource(t *testing.T) {
	env := readEnv(t)
	result := env.Mem("mv", "missing.md", "new.md")
	if result.Exit != 4 {
		t.Fatalf("exit = %d, stderr=%s", result.Exit, result.Stderr)
	}
	envelope, ok := result.Error["error"].(map[string]any)
	if !ok {
		t.Fatalf("stderr is not an error envelope: %s", result.Stderr)
	}
	if code := envelope["code"]; code != "local_file_missing" {
		t.Fatalf("error = %v", result.Error)
	}
}
