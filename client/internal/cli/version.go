package cli

import (
	"fmt"
	"runtime/debug"
	"strings"
)

// Version and Commit are overridden by the release build with -ldflags. A
// normal development build falls back to the Go VCS stamp, then to "unknown".
var (
	Version = "0.2.0"
	Commit  string
)

func shortCommit(value string) string {
	value = strings.TrimSpace(value)
	if len(value) > 12 {
		return value[:12]
	}
	if value == "" {
		return "unknown"
	}
	return value
}

func buildCommit() string {
	if Commit != "" {
		return shortCommit(Commit)
	}
	info, ok := debug.ReadBuildInfo()
	if ok {
		for _, setting := range info.Settings {
			if setting.Key == "vcs.revision" {
				return shortCommit(setting.Value)
			}
		}
	}
	return "unknown"
}

func printVersion() {
	fmt.Printf("mem %s (go, commit %s)\n", Version, buildCommit())
}
