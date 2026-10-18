package devicehome

import (
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func setHome(t *testing.T) string {
	t.Helper()
	home := t.TempDir()
	t.Setenv("HOME", home)
	t.Setenv("USERPROFILE", home)
	return home
}

func TestInitCreatesAndVerifiesIdentity(t *testing.T) {
	home := setHome(t)
	target, err := Init("dev", "d1")
	if err != nil {
		t.Fatal(err)
	}
	if target != filepath.Join(home, ".device", "DEVICE.md") {
		t.Fatalf("path = %q", target)
	}
	data, err := os.ReadFile(target)
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"device: dev\n", "device_id: d1\n", "scope: device\n", "share: never\n"} {
		if !strings.Contains(string(data), want) {
			t.Errorf("DEVICE.md missing %q:\n%s", want, data)
		}
	}
	if again, err := Init("dev", "d1"); err != nil || again != target {
		t.Fatalf("second init = %q, %v", again, err)
	}
	data = []byte(strings.Replace(string(data), "device: dev", "device: other", 1))
	if err := os.WriteFile(target, data, 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := Init("dev", "d1"); err == nil {
		t.Fatal("accepted another device's DEVICE.md")
	}
}

func TestIdentityRefusalDoesNotMutateGlobalConfig(t *testing.T) {
	home := setHome(t)
	dir := filepath.Join(home, ".device")
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "DEVICE.md"), []byte("---\ndevice: other\ndevice_id: other\n---\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := Init("dev", "d1"); err == nil {
		t.Fatal("accepted another device")
	}
	cmd := exec.Command("git", "config", "--global", "--get", "core.excludesFile")
	cmd.Env = os.Environ()
	if out, err := cmd.Output(); err == nil && strings.TrimSpace(string(out)) != "" {
		t.Fatalf("global config was mutated before identity refusal: %q", out)
	}
}

func TestEnsureGlobalIgnoreExpandsTilde(t *testing.T) {
	home := setHome(t)
	work := t.TempDir()
	t.Chdir(work)
	cmd := exec.Command("git", "config", "--global", "core.excludesFile", "~/.gitignore_global")
	cmd.Env = os.Environ()
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("seed git config: %v: %s", err, out)
	}
	if err := EnsureGlobalIgnore(home); err != nil {
		t.Fatal(err)
	}
	ignore := filepath.Join(home, ".gitignore_global")
	data, err := os.ReadFile(ignore)
	if err != nil {
		t.Fatalf("real home ignore file was not written: %v", err)
	}
	if !strings.Contains(string(data), ".device/") {
		t.Fatalf("ignore file missing .device/: %q", data)
	}
	if _, err := os.Stat(filepath.Join(work, "~")); err == nil {
		t.Fatal("created a literal ~ directory")
	}
}

func TestCloudSyncMatchesComponentsOnly(t *testing.T) {
	if err := checkUnsyncedHome("/home/me/dropbox-notes"); err != nil {
		t.Fatalf("substring in a path component must not count: %v", err)
	}
	if err := checkUnsyncedHome("/home/me/Dropbox"); err == nil {
		t.Fatal("a real Dropbox component was accepted")
	}
}
