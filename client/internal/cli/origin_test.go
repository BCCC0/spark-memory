package cli

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestRequireOriginInside(t *testing.T) {
	root := t.TempDir()
	inside := filepath.Join(root, "notes", "a.md")
	if err := os.MkdirAll(filepath.Dir(inside), 0755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(inside, []byte("body\n"), 0644); err != nil {
		t.Fatal(err)
	}
	if got, err := requireOriginInside(inside, root); err != nil || got == "" {
		t.Fatalf("inside file: got %q, %v", got, err)
	}

	outside := filepath.Join(filepath.Dir(root), "outside.md")
	if err := os.WriteFile(outside, []byte("body\n"), 0644); err != nil {
		t.Fatal(err)
	}
	if _, err := requireOriginInside(outside, root); err == nil {
		t.Fatal("outside file was accepted")
	}

	dotdot := filepath.Join(root, "notes", "..", "..", "outside.md")
	if _, err := requireOriginInside(dotdot, root); err == nil {
		t.Fatal(".. escape was accepted")
	}
}

func TestRequireOriginInsideResolvesSymlinks(t *testing.T) {
	if runtime.GOOS == "windows" {
		// Creating symlinks on Windows usually needs Developer Mode or an
		// elevated token; skip rather than make the suite environment-dependent.
		t.Skip("symlink creation is privilege-dependent on Windows")
	}
	root := t.TempDir()
	outsideDir := t.TempDir()
	outside := filepath.Join(outsideDir, "outside.md")
	if err := os.WriteFile(outside, []byte("body\n"), 0644); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(root, "escape.md")
	if err := os.Symlink(outside, link); err != nil {
		t.Skipf("symlinks unavailable: %v", err)
	}
	if _, err := requireOriginInside(link, root); err == nil {
		t.Fatal("symlink escape was accepted")
	}
}

func TestIsUNC(t *testing.T) {
	if !isUNC(`\\server\share\x.md`) {
		t.Fatal("UNC path was not recognised")
	}
	if isUNC(`C:\src\x.md`) {
		t.Fatal("drive path was mistaken for UNC")
	}
	if isUNC(`\\?\C:\src\x.md`) {
		t.Fatal("extended-length local path was mistaken for UNC")
	}
	if !isUNC(`\\?\UNC\server\share\x.md`) {
		t.Fatal("extended-length UNC path was not recognised")
	}
}
