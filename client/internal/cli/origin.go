package cli

import (
	"os"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/BCCC0/spark-memory/client/internal/memerr"
)

// isUNC reports whether path names a Windows network/device share rather than a
// local drive. A long-path prefix (\\?\) is a local path written in extended
// form; everything else beginning with two separators is outside the project
// root's path space and is refused by policy.
func isUNC(path string) bool {
	if strings.HasPrefix(path, `\\?\UNC\`) {
		return true
	}
	return strings.HasPrefix(path, `\\`) && !strings.HasPrefix(path, `\\?\`)
}

// requireOriginInside canonicalises path and root and returns path only when it
// is inside root. The project root is the directory holding .mem.toml; symlinks
// are resolved so an inside-looking link cannot escape it.
func requireOriginInside(path, root string) (string, error) {
	detail := func() map[string]any {
		return map[string]any{
			"path": path,
			"root": root,
			"hint": "copy the file under the project root, use `mem pointer add` for a reference, or pass --no-origin --as <id>",
		}
	}
	absPath, err := filepath.Abs(path)
	if err != nil {
		return "", memerr.New("origin_outside_root", "cannot resolve "+path+": "+err.Error(), detail())
	}
	resolved, err := filepath.EvalSymlinks(absPath)
	if err != nil {
		return "", memerr.New("origin_outside_root", "cannot resolve "+path+": "+err.Error(), detail())
	}
	absRoot, err := filepath.Abs(root)
	if err != nil {
		return "", memerr.New("origin_outside_root", "cannot resolve project root "+root+": "+err.Error(), detail())
	}
	resolvedRoot, err := filepath.EvalSymlinks(absRoot)
	if err != nil {
		return "", memerr.New("origin_outside_root", "cannot resolve project root "+root+": "+err.Error(), detail())
	}
	resolved = filepath.Clean(resolved)
	resolvedRoot = filepath.Clean(resolvedRoot)

	if runtime.GOOS == "windows" && (isUNC(resolved) || isUNC(resolvedRoot)) {
		return "", memerr.New("origin_outside_root", path+" is a UNC path; project origins must be local paths", detail())
	}

	comparePath, compareRoot := resolved, resolvedRoot
	if runtime.GOOS == "windows" {
		comparePath, compareRoot = strings.ToLower(comparePath), strings.ToLower(compareRoot)
	}
	rel, err := filepath.Rel(compareRoot, comparePath)
	if err != nil || rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) || filepath.IsAbs(rel) {
		return "", memerr.New("origin_outside_root", path+" is outside the project root "+resolvedRoot, detail())
	}
	return resolved, nil
}
