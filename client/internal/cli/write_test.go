package cli

import (
	"os"
	"path/filepath"
	"testing"
)

func TestAddRejectsAnOutsideOrigin(t *testing.T) {
	env := readEnv(t)
	env.Daemon.Handle("GET", "/v1/policy", 200, map[string]any{
		"inline_extensions": []any{".md"},
		"max_upload_bytes":  100000,
	})

	outside := filepath.Join(t.TempDir(), "outside.md")
	if err := os.WriteFile(outside, []byte("---\nsummary: outside\n---\nbody\n"), 0644); err != nil {
		t.Fatal(err)
	}

	result := env.Mem("add", outside, "--as", "outside")
	if result.Exit != 4 {
		t.Fatalf("exit = %d, stderr=%s", result.Exit, result.Stderr)
	}
	envelope, ok := result.Error["error"].(map[string]any)
	if !ok {
		t.Fatalf("stderr is not an error envelope: %s", result.Stderr)
	}
	if code := envelope["code"]; code != "origin_outside_root" {
		t.Fatalf("error code = %v, stderr=%s", code, result.Stderr)
	}
}
