package ids

import (
	"fmt"
	"strings"
	"unicode"

	"github.com/BCCC0/spark-memory/client/internal/encoding"
	"github.com/BCCC0/spark-memory/client/internal/memerr"
	"github.com/BCCC0/spark-memory/client/internal/pyrepr"
)

func bad(message string) error { return memerr.New("schema_invalid", message, nil) }

func Segment(value string, limit int, kind string) (string, error) {
	if value == "" {
		return "", bad("empty " + kind)
	}
	if value == "." || value == ".." {
		return "", bad(fmt.Sprintf("%s may not be %s", kind, pyrepr.String(value)))
	}
	if strings.HasPrefix(value, ".") || strings.HasSuffix(value, ".") || strings.Contains(value, "..") {
		return "", bad(kind + " may not start/end with a dot or contain '..'")
	}
	if n := encoding.RuneCount(value); n > limit {
		return "", bad(fmt.Sprintf("%s %s is %d chars; the limit is %d", kind, pyrepr.String(value), n, limit))
	}
	// Python's _why_not checks the whole segment for an uppercase letter before
	// it reports any other character, so the diagnostic names uppercase even
	// when an earlier character is also illegal.
	for _, r := range value {
		if unicode.IsUpper(r) {
			return "", bad(fmt.Sprintf("%s %s is invalid: contains uppercase (ids are lowercase)", kind, pyrepr.String(value)))
		}
	}
	for _, r := range value {
		why := ""
		switch r {
		case '—':
			why = "contains an em dash — (use a plain '-')"
		case ' ':
			why = "contains a space"
		case '_':
			why = "contains an underscore"
		case '/':
			why = "contains '/', which separates segments"
		default:
			if !strings.ContainsRune("abcdefghijklmnopqrstuvwxyz0123456789-.", r) {
				why = fmt.Sprintf("contains %s, which is not allowed", pyrepr.String(string(r)))
			}
		}
		if why != "" {
			return "", bad(fmt.Sprintf("%s %s is invalid: %s", kind, pyrepr.String(value), why))
		}
	}
	if strings.HasPrefix(value, "-") || strings.HasSuffix(value, "-") {
		return "", bad(fmt.Sprintf("%s %s is invalid: starts or ends with '-'", kind, pyrepr.String(value)))
	}
	return value, nil
}

func Validate(id string) (string, error) {
	id = strings.Trim(strings.ReplaceAll(id, "\\", "/"), "/")
	if id == "" {
		return "", bad("empty id")
	}
	if id == "PROJECT" {
		return id, nil
	}
	parts := strings.Split(id, "/")
	for i, part := range parts {
		kind, limit := "directory", 40
		if i == len(parts)-1 {
			kind, limit = "filename", 80
		}
		if _, err := Segment(part, limit, kind); err != nil {
			return "", err
		}
	}
	return id, nil
}

func Project(value string) (string, error) {
	value = strings.TrimSpace(value)
	if value == "" {
		return "", bad("missing project")
	}
	if strings.ContainsAny(value, "/\\") {
		return "", bad(fmt.Sprintf("project %s must be a single name", pyrepr.String(value)))
	}
	return Segment(value, 40, "project name")
}

func FromPath(path string) string {
	path = strings.ReplaceAll(path, "\\", "/")
	for strings.HasPrefix(path, "./") {
		path = strings.TrimPrefix(path, "./")
	}
	if strings.HasSuffix(strings.ToLower(path), ".md") {
		path = path[:len(path)-3]
	}
	return path
}
