package paths

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"
)

func TestPathTranslationMatchesPython(t *testing.T) {
	cases := []struct {
		in, windows, wsl string
	}{
		{`D:relative`, "", ""},
		{`d:foo`, "", ""},
		{`D:`, "D:", "/mnt/d"},
		{`D:/`, `D:\`, "/mnt/d"},
		{`D:\`, `D:\`, "/mnt/d"},
		{`C:/a//b`, `C:\a\b`, "/mnt/c/a/b"},
		{`C:/a/./b`, `C:\a\b`, "/mnt/c/a/b"},
		{`C:/a/../b`, `C:\a\..\b`, "/mnt/c/a/../b"},
		{`c:/Work/a.md`, `c:\Work\a.md`, "/mnt/c/Work/a.md"},
		{`/mnt/d`, `D:\`, "/mnt/d"},
		{`/mnt/d/`, `D:\`, "/mnt/d/"},
		{`/mnt/d/src/x`, `D:\src\x`, "/mnt/d/src/x"},
		{`\mnt\d\x`, `D:\x`, "/mnt/d/x"},
		{`/mnt/e/`, `E:\`, "/mnt/e/"},
		{`relative/x`, "", ""},
		{`/home/x`, "", ""},
		{`C:////a`, `C:\a`, "/mnt/c/a"},
	}
	for _, tc := range cases {
		t.Run(tc.in, func(t *testing.T) {
			if got := ToWindows(tc.in); got != tc.windows {
				t.Errorf("ToWindows(%q) = %q, want %q", tc.in, got, tc.windows)
			}
			if got := ToWSL(tc.in); got != tc.wsl {
				t.Errorf("ToWSL(%q) = %q, want %q", tc.in, got, tc.wsl)
			}
		})
	}
}

func TestResolveExistingFileOnly(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "note.md")
	if err := os.WriteFile(file, []byte("x"), 0600); err != nil {
		t.Fatal(err)
	}
	if got := Resolve(file); got != file {
		t.Fatalf("resolve file: %q", got)
	}
	if got := Resolve(dir); got != "" {
		t.Fatalf("resolved directory as file: %q", got)
	}
	if got := Resolve(filepath.Join(dir, "missing")); got != "" {
		t.Fatalf("fabricated missing path: %q", got)
	}
}

func TestTranslateCurrentPlatform(t *testing.T) {
	if runtime.GOOS == "windows" {
		if got := Translate("/mnt/d/src/note.md"); got != `D:\src\note.md` {
			t.Fatalf("translation: %q", got)
		}
	} else {
		if got := Translate(`D:\src\note.md`); got != "/mnt/d/src/note.md" {
			t.Fatalf("translation: %q", got)
		}
	}
}

func TestExpandUser(t *testing.T) {
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	if got := ExpandUser("~/note.md"); got != filepath.Join(home, "note.md") {
		t.Fatalf("ExpandUser(~/note.md) = %q", got)
	}
}
