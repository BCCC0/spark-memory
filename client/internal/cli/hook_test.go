package cli

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
	"time"
)

func TestHookTimeoutScalesWithBatch(t *testing.T) {
	cases := map[int]time.Duration{
		1:     180 * time.Second,
		101:   180 * time.Second,
		1000:  560 * time.Second,
		10000: 900 * time.Second,
	}
	for count, want := range cases {
		if got := hookTimeout(count); got != want {
			t.Fatalf("hookTimeout(%d) = %s, want %s", count, got, want)
		}
	}
}

func TestHookInstallPreservesUserContent(t *testing.T) {
	repo := t.TempDir()
	cmd := exec.Command("git", "init", "-q", repo)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Skipf("git unavailable: %s: %v", out, err)
	}
	dir := filepath.Join(repo, ".git", "hooks")
	target := filepath.Join(dir, "post-commit")
	original := "#!/bin/sh\necho USER_HOOK_RAN\n"
	if err := os.WriteFile(target, []byte(original), 0755); err != nil {
		t.Fatal(err)
	}
	if _, err := installHook(repo, false); err != nil {
		t.Fatal(err)
	}
	if _, err := installHook(repo, false); err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(target)
	if err != nil {
		t.Fatal(err)
	}
	text := string(data)
	if strings.Count(text, markBegin) != 1 || !strings.Contains(text, "echo USER_HOOK_RAN") || !strings.Contains(text, "hook update") || !strings.Contains(text, "MEM_BIN") {
		t.Fatalf("bad shim: %s", text)
	}
	if _, err := installHook(repo, true); err != nil {
		t.Fatal(err)
	}
	data, err = os.ReadFile(target)
	if err != nil {
		t.Fatal(err)
	}
	if string(data) != original {
		t.Fatalf("user hook changed after uninstall: %q", data)
	}
}

func TestHookInstallMakesExistingHookExecutable(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX mode bits are not meaningful on Windows")
	}
	repo := t.TempDir()
	if out, err := exec.Command("git", "init", "-q", repo).CombinedOutput(); err != nil {
		t.Skipf("git unavailable: %s: %v", out, err)
	}
	target := filepath.Join(repo, ".git", "hooks", "post-commit")
	if err := os.WriteFile(target, []byte("#!/bin/sh\necho USER\n"), 0644); err != nil {
		t.Fatal(err)
	}
	if _, err := installHook(repo, false); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(target)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode().Perm()&0111 == 0 {
		t.Fatalf("hook mode is %o, want executable", info.Mode().Perm())
	}
}

func TestHookUninstallRemovesShebangOnlyFile(t *testing.T) {
	repo := t.TempDir()
	if out, err := exec.Command("git", "init", "-q", repo).CombinedOutput(); err != nil {
		t.Skipf("git unavailable: %s: %v", out, err)
	}
	target := filepath.Join(repo, ".git", "hooks", "post-merge")
	if err := os.WriteFile(target, []byte("#!/usr/bin/env sh\n"), 0755); err != nil {
		t.Fatal(err)
	}
	// Append only our block, exactly as install would leave it.
	if _, err := installHook(repo, false); err != nil {
		t.Fatal(err)
	}
	if _, err := installHook(repo, true); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(target); !os.IsNotExist(err) {
		t.Fatalf("shebang-only hook was not removed: %v", err)
	}
}
