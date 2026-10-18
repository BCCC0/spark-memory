// Package hostinfo renders the host facts the Python client reports through
// platform.system()/platform.machine(). Go's runtime values are close but not
// identical (notably amd64 vs AMD64/x86_64), and the server stores these in the
// device registry, so both clients must send the same spelling.
package hostinfo

import "runtime"

// OS matches Python's platform.system().lower().
func OS() string { return runtime.GOOS }

// Machine matches Python's platform.machine().
func Machine() string {
	switch runtime.GOARCH {
	case "amd64":
		if runtime.GOOS == "windows" {
			return "AMD64"
		}
		return "x86_64"
	case "arm64":
		switch runtime.GOOS {
		case "windows":
			return "ARM64"
		case "darwin":
			return "arm64"
		default:
			return "aarch64"
		}
	case "386":
		if runtime.GOOS == "windows" {
			return "x86"
		}
		return "i686"
	case "arm":
		return "armv7l"
	default:
		return runtime.GOARCH
	}
}
