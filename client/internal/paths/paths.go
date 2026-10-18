package paths

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"
)

func letter(v byte) bool { return v >= 'A' && v <= 'Z' || v >= 'a' && v <= 'z' }

// looksLikeWindowsPath mirrors _DRIVE_RE: a drive letter followed by a
// separator or the end of the string. "D:relative" is drive-relative, not an
// absolute Windows path, and must not be translated.
func looksLikeWindowsPath(value string) bool {
	return len(value) >= 2 && value[1] == ':' && letter(value[0]) &&
		(len(value) == 2 || value[2] == '/' || value[2] == '\\')
}

// wslMount returns the drive letter and the path under /mnt/<letter>, if value
// (with backslashes normalised) is a WSL automount.
func wslMount(value string) (byte, string, bool) {
	posix := strings.ReplaceAll(value, "\\", "/")
	if len(posix) < 6 || !strings.HasPrefix(posix, "/mnt/") || !letter(posix[5]) {
		return 0, "", false
	}
	if len(posix) > 6 && posix[6] != '/' {
		return 0, "", false
	}
	return posix[5], posix[6:], true
}

func segments(value string) []string {
	var out []string
	for _, part := range strings.FieldsFunc(value, func(r rune) bool { return r == '/' || r == '\\' }) {
		if part == "" || part == "." {
			continue
		}
		out = append(out, part)
	}
	return out
}

func normalizeWindows(value string) string {
	drive := string(value[0])
	tail := segments(value[2:])
	if len(tail) == 0 {
		if len(value) == 2 {
			return drive + ":"
		}
		return drive + ":\\"
	}
	return drive + ":\\" + strings.Join(tail, "\\")
}

func ToWindows(value string) string {
	value = strings.TrimSpace(value)
	if looksLikeWindowsPath(value) {
		return normalizeWindows(value)
	}
	drive, tail, ok := wslMount(value)
	if !ok {
		return ""
	}
	parts := segments(tail)
	if len(parts) == 0 {
		return strings.ToUpper(string(drive)) + ":\\"
	}
	return strings.ToUpper(string(drive)) + ":\\" + strings.Join(parts, "\\")
}

func ToWSL(value string) string {
	value = strings.TrimSpace(value)
	if _, _, ok := wslMount(value); ok {
		return strings.ReplaceAll(value, "\\", "/")
	}
	if !looksLikeWindowsPath(value) {
		return ""
	}
	drive := strings.ToLower(value[:1])
	parts := segments(value[2:])
	if len(parts) == 0 {
		return "/mnt/" + drive
	}
	return "/mnt/" + drive + "/" + strings.Join(parts, "/")
}

func Translate(value string) string {
	if runtime.GOOS == "windows" {
		return ToWindows(value)
	}
	return ToWSL(value)
}

// ExpandUser expands a leading "~" or "~/" using the current user's home.
func ExpandUser(value string) string {
	if value == "~" {
		if home, err := os.UserHomeDir(); err == nil {
			return home
		}
		return value
	}
	if strings.HasPrefix(value, "~/") || strings.HasPrefix(value, `~\`) {
		if home, err := os.UserHomeDir(); err == nil {
			return filepath.Join(home, value[2:])
		}
	}
	return value
}

func Resolve(value string) string {
	for _, candidate := range []string{value, Translate(value)} {
		if candidate == "" {
			continue
		}
		candidate = ExpandUser(candidate)
		info, err := os.Stat(candidate)
		if err == nil && info.Mode().IsRegular() {
			if runtime.GOOS == "windows" && looksLikeWindowsPath(candidate) {
				return normalizeWindows(candidate)
			}
			return candidate
		}
	}
	return ""
}
