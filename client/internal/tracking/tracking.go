package tracking

import (
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strings"
	"time"

	"github.com/BCCC0/spark-memory/client/internal/config"
	"github.com/BCCC0/spark-memory/client/internal/memerr"
)

func Git(repo string, args ...string) ([]byte, error) {
	cmd := exec.Command("git", args...)
	cmd.Dir = repo
	out, err := cmd.Output()
	if err != nil {
		return nil, memerr.New("invalid_request", "git "+strings.Join(args, " ")+" failed", nil)
	}
	return out, nil
}
func Root(dir string) (string, error) {
	out, err := Git(dir, "rev-parse", "--show-toplevel")
	if err != nil {
		return "", err
	}
	return strings.TrimSpace(string(out)), nil
}
func Matches(patterns []string, file string) bool {
	included := false
	for _, raw := range patterns {
		if raw == "" || strings.HasPrefix(raw, "!") {
			continue
		}
		if match(raw, file) {
			included = true
		}
	}
	if !included {
		return false
	}
	for _, raw := range patterns {
		if strings.HasPrefix(raw, "!") && match(strings.TrimPrefix(raw, "!"), file) {
			return false
		}
	}
	return true
}
func match(pattern, file string) bool {
	pattern = strings.ReplaceAll(strings.TrimSpace(pattern), "\\", "/")
	file = strings.ReplaceAll(file, "\\", "/")
	pattern = strings.TrimPrefix(pattern, "./")
	if strings.HasSuffix(pattern, "/") {
		pattern += "**"
	}
	if !strings.Contains(pattern, "/") {
		pattern = "**/" + pattern
	}
	return matchSegments(parts(pattern), parts(file))
}

func parts(value string) []string {
	var out []string
	for _, part := range strings.Split(value, "/") {
		if part == "" || part == "." {
			continue
		}
		out = append(out, part)
	}
	return out
}

func matchSegments(p, f []string) bool {
	if len(p) == 0 {
		return len(f) == 0
	}
	if p[0] == "**" {
		return matchSegments(p[1:], f) || len(f) > 0 && matchSegments(p, f[1:])
	}
	if len(f) == 0 {
		return false
	}
	return matchSegment(p[0], f[0]) && matchSegments(p[1:], f[1:])
}

// matchSegment implements Python fnmatch's segment semantics: *, ?, [seq] and
// [!seq]. Go's path.Match uses [^seq] and treats backslash as an escape, so it
// cannot be used directly.
func matchSegment(pattern, value string) bool {
	var out strings.Builder
	out.WriteString("^")
	for i := 0; i < len(pattern); i++ {
		switch pattern[i] {
		case '*':
			out.WriteString(".*")
		case '?':
			out.WriteString(".")
		case '[':
			end := i + 1
			if end < len(pattern) && pattern[end] == '!' {
				end++
			}
			if end < len(pattern) && pattern[end] == ']' {
				end++
			}
			for end < len(pattern) && pattern[end] != ']' {
				end++
			}
			if end >= len(pattern) {
				out.WriteString(`\[`)
				continue
			}
			class := pattern[i+1 : end]
			negated := strings.HasPrefix(class, "!")
			if negated {
				class = class[1:]
			}
			class = strings.ReplaceAll(class, `\`, `\\`)
			if negated {
				out.WriteString("[^")
			} else {
				out.WriteString("[")
			}
			out.WriteString(class)
			out.WriteString("]")
			i = end
		default:
			out.WriteString(regexp.QuoteMeta(string(pattern[i])))
		}
	}
	out.WriteString("$")
	re, err := regexp.Compile(out.String())
	return err == nil && re.MatchString(value)
}
func Selected(patterns, files []string) []string {
	out := []string{}
	for _, file := range files {
		if Matches(patterns, file) {
			out = append(out, file)
		}
	}
	return out
}
func SplitNUL(data []byte) []string {
	out := []string{}
	for _, s := range strings.Split(string(data), "\x00") {
		if s != "" {
			out = append(out, s)
		}
	}
	return out
}
func Paths(repo, rev, since string) ([]string, error) {
	if rev == since {
		return []string{}, nil
	}
	args := []string{"diff-tree", "--no-commit-id", "--name-only", "-r", "-z", "--root"}
	if since != "" {
		args = append(args, since+".."+rev)
	} else {
		args = append(args, rev)
	}
	out, err := Git(repo, args...)
	if err != nil {
		return nil, err
	}
	return SplitNUL(out), nil
}

type State struct {
	Version int              `json:"version"`
	Repos   map[string]Entry `json:"repos"`
}
type Entry struct {
	Last    string `json:"last"`
	Path    string `json:"path"`
	Updated string `json:"updated"`
}

func statePath() string { return filepath.Join(config.Dir(), "hook-state.json") }
func readState() State {
	data, _ := os.ReadFile(statePath())
	var s State
	_ = json.Unmarshal(data, &s)
	if s.Repos == nil {
		s.Repos = map[string]Entry{}
	}
	return s
}
func key(repo string) string {
	abs, err := filepath.Abs(repo)
	if err != nil {
		abs = repo
	}
	if resolved, err := filepath.EvalSymlinks(abs); err == nil {
		abs = resolved
	}
	return filepath.ToSlash(abs)
}
func Watermark(repo string) string { return readState().Repos[key(repo)].Last }
func SetWatermark(repo, rev string) error {
	s := readState()
	s.Version = 1
	s.Repos[key(repo)] = Entry{Last: rev, Path: repo, Updated: time.Now().UTC().Format(time.RFC3339)}
	data, err := json.MarshalIndent(s, "", "  ")
	if err != nil {
		return err
	}
	data = append(data, '\n')
	if err := os.MkdirAll(config.Dir(), 0700); err != nil {
		return err
	}
	tmp := statePath() + ".tmp"
	if err := os.WriteFile(tmp, data, 0600); err != nil {
		return err
	}
	return os.Rename(tmp, statePath())
}
