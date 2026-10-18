// Package devicehome owns the private per-device ~/.device/DEVICE.md.
//
// The file is local-only: it is never uploaded to project memory. Its identity
// check exists so a DEVICE.md copied or synced from another machine is not
// adopted. Identity is the permanent device_id plus its unique alias; hostname
// is diagnostic only, because it changes across DHCP leases and domain renames.
package devicehome

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/BCCC0/spark-memory/client/internal/hostinfo"
	"github.com/BCCC0/spark-memory/client/internal/memerr"
	"github.com/BCCC0/spark-memory/client/internal/paths"
)

var syncNames = []string{"onedrive", "dropbox", "google drive", "googledrive", "icloud drive", "icloud"}

// Home is the platform's real home directory. It deliberately uses
// os.UserHomeDir so it agrees with os.Path.home() in Python on Windows, where
// Path.home() reads USERPROFILE rather than a shell's HOME.
func Home() (string, error) {
	home, err := os.UserHomeDir()
	if err != nil {
		return "", memerr.New("internal_error", "cannot determine the home directory: "+err.Error(), nil)
	}
	home, err = filepath.Abs(home)
	if err != nil {
		return "", memerr.New("internal_error", "cannot resolve the home directory: "+err.Error(), nil)
	}
	return home, nil
}

// IsDeviceHome reports whether path is inside <home>/.device.
func IsDeviceHome(path string) bool {
	home, err := Home()
	if err != nil {
		return false
	}
	base := filepath.Join(home, ".device")
	rel, err := filepath.Rel(base, path)
	return err == nil && rel != ".." && !strings.HasPrefix(rel, ".."+string(os.PathSeparator))
}

// Init creates or verifies ~/.device/DEVICE.md and returns its path.
func Init(alias, deviceID string) (string, error) {
	home, err := Home()
	if err != nil {
		return "", err
	}
	if err := checkUnsyncedHome(home); err != nil {
		return "", err
	}
	dir := filepath.Join(home, ".device")
	if info, err := os.Lstat(dir); err == nil && info.Mode()&os.ModeSymlink != 0 {
		return "", memerr.New("invalid_request", "~/.device is a symlink; refusing to write outside the device home", nil)
	}
	target := filepath.Join(dir, "DEVICE.md")
	if data, err := os.ReadFile(target); err == nil {
		// Verify identity before mutating global Git configuration. A refusal
		// must leave the machine exactly as it was.
		identity, err := parseIdentity(string(data))
		if err != nil {
			return "", memerr.New("invalid_request", "cannot verify existing "+target+": "+err.Error(), nil)
		}
		if identity["device"] != alias || identity["device_id"] != deviceID {
			return "", memerr.New("invalid_request", "this is not this device's home", nil)
		}
		return target, nil
	}
	if err := rejectTrackedPath(home, dir); err != nil {
		return "", err
	}
	if err := EnsureGlobalIgnore(home); err != nil {
		return "", err
	}
	if err := os.MkdirAll(dir, 0700); err != nil {
		return "", memerr.New("internal_error", "cannot create "+dir+": "+err.Error(), nil)
	}
	host, _ := os.Hostname()
	content := fmt.Sprintf("---\ndevice: %s\ndevice_id: %s\nhostname: %s\nos: %s\narch: %s\nscope: device\nshare: never\n---\n\n# Device home\n\nDevice-specific facts and pointers for this machine.\n",
		alias, deviceID, host, hostinfo.OS(), hostinfo.Machine())
	file, err := os.OpenFile(target, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0600)
	if err != nil {
		if os.IsExist(err) {
			return "", memerr.New("invalid_request", "DEVICE.md appeared during initialization; refusing to overwrite it", nil)
		}
		return "", memerr.New("internal_error", "cannot write "+target+": "+err.Error(), nil)
	}
	_, writeErr := file.WriteString(content)
	closeErr := file.Close()
	if writeErr != nil {
		return "", memerr.New("internal_error", "cannot write "+target+": "+writeErr.Error(), nil)
	}
	if closeErr != nil {
		return "", memerr.New("internal_error", "cannot write "+target+": "+closeErr.Error(), nil)
	}
	return target, nil
}

func parseIdentity(text string) (map[string]string, error) {
	if !strings.HasPrefix(text, "---") {
		return nil, fmt.Errorf("missing frontmatter")
	}
	parts := strings.SplitN(text, "---", 3)
	if len(parts) < 3 {
		return nil, fmt.Errorf("unterminated frontmatter")
	}
	identity := map[string]string{}
	for _, line := range strings.Split(parts[1], "\n") {
		line = strings.TrimSpace(strings.TrimSuffix(line, "\r"))
		if line == "" {
			continue
		}
		key, value, ok := strings.Cut(line, ":")
		if !ok {
			continue
		}
		identity[strings.TrimSpace(key)] = strings.TrimSpace(value)
	}
	return identity, nil
}

func checkUnsyncedHome(home string) error {
	normalized := strings.ToLower(evaluated(home))
	parts := strings.Split(filepath.ToSlash(normalized), "/")
	inParts := map[string]bool{}
	for _, part := range parts {
		inParts[part] = true
	}
	for _, name := range syncNames {
		if inParts[name] {
			return memerr.New("invalid_request", "home directory appears cloud-synced: "+home+"; choose a private unsynced home before initializing device data", nil)
		}
	}
	for _, name := range []string{"OneDrive", "Dropbox", "GOOGLE_DRIVE"} {
		value := os.Getenv(name)
		if value == "" {
			continue
		}
		abs, err := filepath.Abs(value)
		if err != nil {
			continue
		}
		abs = evaluated(abs)
		if strings.EqualFold(normalized, strings.ToLower(abs)) ||
			strings.HasPrefix(strings.ToLower(normalized), strings.ToLower(abs)+string(os.PathSeparator)) {
			return memerr.New("invalid_request", "home directory appears cloud-synced: "+home+"; choose a private unsynced home before initializing device data", nil)
		}
	}
	return nil
}

func evaluated(value string) string {
	if resolved, err := filepath.EvalSymlinks(value); err == nil {
		return resolved
	}
	return value
}

func git(cwd string, args ...string) (string, error) {
	command := exec.Command("git", args...)
	command.Dir = cwd
	out, err := command.Output()
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(out)), nil
}

func rejectTrackedPath(home, dir string) error {
	root, err := git(home, "rev-parse", "--show-toplevel")
	if err != nil || root == "" {
		return nil
	}
	root, _ = filepath.Abs(root)
	rel, err := filepath.Rel(root, dir)
	if err != nil || strings.HasPrefix(rel, "..") {
		return nil
	}
	tracked, err := git(root, "ls-files", "--", filepath.ToSlash(rel))
	if err == nil && tracked != "" {
		return memerr.New("invalid_request", "~/.device/ is already tracked by Git; refusing to write device data", nil)
	}
	return nil
}

// EnsureGlobalIgnore puts .device/ in the user's global Git excludes file. It
// preserves an existing core.excludesFile setting instead of rewriting it.
func EnsureGlobalIgnore(home string) error {
	configured, err := git(home, "config", "--global", "--get", "core.excludesFile")
	path := ""
	if err == nil && configured != "" {
		path = paths.ExpandUser(configured)
	}
	writeConfig := path == ""
	if path == "" {
		path = filepath.Join(home, ".config", "git", "ignore")
	}
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return memerr.New("internal_error", "cannot create "+filepath.Dir(path)+": "+err.Error(), nil)
	}
	data, _ := os.ReadFile(path)
	text := string(data)
	present := false
	for _, line := range strings.Split(text, "\n") {
		switch strings.TrimSpace(line) {
		case ".device/", "/.device/":
			present = true
		}
	}
	if !present {
		if text != "" && !strings.HasSuffix(text, "\n") {
			text += "\n"
		}
		text += ".device/\n"
		if err := os.WriteFile(path, []byte(text), 0644); err != nil {
			return memerr.New("internal_error", "cannot write "+path+": "+err.Error(), nil)
		}
	}
	if writeConfig {
		if _, err := git(home, "config", "--global", "core.excludesFile", path); err != nil {
			return memerr.New("internal_error", "cannot set the global Git excludes file: "+err.Error(), nil)
		}
	}
	return nil
}
