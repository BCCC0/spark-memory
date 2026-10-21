package cli

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"time"

	"github.com/BCCC0/spark-memory/client/internal/config"
	"github.com/BCCC0/spark-memory/client/internal/ids"
	"github.com/BCCC0/spark-memory/client/internal/keys"
	"github.com/BCCC0/spark-memory/client/internal/memerr"
	"github.com/BCCC0/spark-memory/client/internal/transport"
)

func moveCommand(o options, c config.Config, cl *transport.Client) (any, string, error) {
	if err := o.count(2, 2); err != nil {
		return nil, "", err
	}
	if o.has("id") {
		if o.flag("expect") != "" {
			return nil, "", memerr.New("usage_error", "--expect is only valid for path moves", nil)
		}
		return moveIDs(o, c, cl)
	}
	if o.flag("expect-head") != "" {
		return nil, "", memerr.New("usage_error", "--expect-head is only valid with --id", nil)
	}
	return movePaths(o, c, cl)
}

func moveIDs(o options, c config.Config, cl *transport.Client) (any, string, error) {
	folder := strings.HasSuffix(o.positional[0], "/")
	if folder != strings.HasSuffix(o.positional[1], "/") {
		return nil, "", memerr.New("invalid_request", "folder moves require trailing '/' on both IDs", nil)
	}
	src, err := ids.Validate(strings.TrimSuffix(o.positional[0], "/"))
	if err != nil {
		return nil, "", err
	}
	dst, err := ids.Validate(strings.TrimSuffix(o.positional[1], "/"))
	if err != nil {
		return nil, "", err
	}
	if folder {
		src += "/"
		dst += "/"
	}
	if o.has("dry-run") {
		result, err := req(cl, "POST", "/v1/mv", nil, map[string]any{
			"project": c.Project, "source": src, "destination": dst,
			"message": emptyToNil(o.flag("message")), "dry_run": true,
		}, headers("", o.flag("expect-head")))
		return result, fmt.Sprintf("would move %s to %s", src, dst), err
	}
	key := keys.Move(src, dst, o.flag("expect-head"), o.flag("message"))
	result, err := req(cl, "POST", "/v1/mv", nil, map[string]any{
		"project": c.Project, "source": src, "destination": dst,
		"message": emptyToNil(o.flag("message")), "dry_run": false,
	}, headers(key, o.flag("expect-head")))
	if err != nil {
		return nil, "", err
	}
	if err := waitJob(cl, stringField(result, "receipt"), intField(result, "seq")); err != nil {
		return nil, "", err
	}
	status, _ := req(cl, "GET", "/v1/status", map[string]string{"project": c.Project}, nil, nil)
	final := object(result)
	final["status"] = "committed"
	final["head"] = object(status)["head"]
	return final, fmt.Sprintf("moved %s to %s", src, dst), nil
}

func movePaths(o options, c config.Config, cl *transport.Client) (any, string, error) {
	if strings.HasSuffix(o.positional[0], "/") || strings.HasSuffix(o.positional[1], "/") {
		return nil, "", memerr.New("invalid_request", "path moves take files, not topic IDs", nil)
	}
	if _, err := os.Stat(o.positional[0]); os.IsNotExist(err) &&
		filepath.Ext(o.positional[0]) == "" && filepath.Ext(o.positional[1]) == "" {
		if _, e1 := ids.Validate(o.positional[0]); e1 == nil {
			if _, e2 := ids.Validate(o.positional[1]); e2 == nil {
				return nil, "", memerr.New("usage_error", "mem mv now moves working files; use `mem mv --id` to move note IDs", map[string]any{
					"source": o.positional[0], "destination": o.positional[1],
				})
			}
		}
	}
	root := filepath.Dir(c.MarkerPath)
	source, err := requireMoveSource(o.positional[0], root)
	if err != nil {
		return nil, "", err
	}
	destination, err := requireMoveDestination(o.positional[1], root, source)
	if err != nil {
		return nil, "", err
	}
	if o.has("dry-run") {
		result, err := repathRequest(cl, c, source, destination, o.flag("expect"), o.flag("message"), true, "")
		return result, fmt.Sprintf("would move %s to %s", source, destination), err
	}
	lookup, err := repathRequest(cl, c, source, destination, o.flag("expect"), o.flag("message"), true, "")
	if err != nil {
		return nil, "", err
	}
	if err := renameLocal(source, destination); err != nil {
		return nil, "", memerr.New("internal_error", "cannot move local file: "+err.Error(), nil)
	}
	key := keys.Repath(source, destination, o.flag("expect"), o.flag("message"))
	result, err := repathRequest(cl, c, source, destination, o.flag("expect"), o.flag("message"), false, key)
	if err == nil {
		err = waitJob(cl, stringField(result, "receipt"), intField(result, "seq"))
	}
	if err != nil {
		if rollback := renameLocal(destination, source); rollback != nil {
			return nil, "", memerr.New("internal_error", "move failed and local rollback failed", map[string]any{
				"rollback_error": rollback.Error(),
			})
		}
		return nil, "", err
	}
	status, _ := req(cl, "GET", "/v1/status", map[string]string{"project": c.Project}, nil, nil)
	final := map[string]any{
		"status": "committed", "id": stringField(lookup, "id"),
		"old_path": source, "new_path": destination, "head": object(status)["head"],
	}
	return final, fmt.Sprintf("moved %s to %s", source, destination), nil
}

func repathRequest(cl *transport.Client, c config.Config, oldPath, newPath, expect, message string, dryRun bool, key string) (any, error) {
	return req(cl, "POST", "/v1/repath", nil, map[string]any{
		"project": c.Project, "old_path": oldPath, "new_path": newPath,
		"message": emptyToNil(message), "dry_run": dryRun,
	}, headers(key, expect))
}

func intField(value any, key string) int {
	if v, ok := object(value)[key].(float64); ok {
		return int(v)
	}
	return 0
}


func waitJob(cl *transport.Client, receipt string, seq int) error {
	deadline := time.Now().Add(flushTimeout)
	for {
		progress, err := req(cl, "GET", "/v1/queue", map[string]string{"through": fmt.Sprint(seq)}, nil, nil)
		if err != nil {
			return err
		}
		if failed, ok := object(progress)["failed"].([]any); ok {
			for _, row := range failed {
				if stringField(row, "receipt") == receipt {
					return memerr.New("queued_write_failed", "move "+receipt+" failed", map[string]any{"through": seq, "failed": failed})
				}
			}
		}
		if object(progress)["pending"] == float64(0) {
			return nil
		}
		if time.Now().After(deadline) {
			return memerr.New("lock_timeout", "timed out waiting for the move to commit", map[string]any{"through": seq})
		}
		time.Sleep(100 * time.Millisecond)
	}
}

func requireMoveSource(path, root string) (string, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	info, err := os.Stat(abs)
	if err != nil || !info.Mode().IsRegular() {
		return "", memerr.New("local_file_missing", "working file is missing: "+path, map[string]any{"path": path})
	}
	resolved, err := requireOriginInside(abs, root)
	if err != nil {
		return "", err
	}
	if marker := nearestMarker(filepath.Dir(resolved)); marker != "" && !samePath(marker, filepath.Join(root, ".mem.toml")) {
		return "", memerr.New("cross_project_move", path+" belongs to another project", map[string]any{"path": path, "other_project_root": filepath.Dir(marker)})
	}
	return resolved, nil
}

func requireMoveDestination(path, root, allowExisting string) (string, error) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	parent, err := filepath.EvalSymlinks(filepath.Dir(abs))
	if err != nil {
		return "", memerr.New("invalid_request", "destination directory does not exist: "+filepath.Dir(path), nil)
	}
	resolved, err := requireOriginInside(parent, root)
	if err != nil {
		return "", err
	}
	destination := filepath.Join(resolved, filepath.Base(abs))
	if info, err := os.Stat(destination); err == nil {
		if !samePath(destination, allowExisting) || info.IsDir() {
			return "", memerr.New("local_path_exists", "destination already exists: "+path, map[string]any{"path": path})
		}
	}
	if marker := nearestMarker(resolved); marker != "" && !samePath(marker, filepath.Join(root, ".mem.toml")) {
		return "", memerr.New("cross_project_move", path+" belongs to another project", map[string]any{"path": path, "other_project_root": filepath.Dir(marker)})
	}
	return destination, nil
}

func nearestMarker(dir string) string {
	for {
		candidate := filepath.Join(dir, ".mem.toml")
		if info, err := os.Stat(candidate); err == nil && !info.IsDir() {
			return candidate
		}
		parent := filepath.Dir(dir)
		if parent == dir {
			return ""
		}
		dir = parent
	}
}

func samePath(a, b string) bool {
	a, b = filepath.Clean(a), filepath.Clean(b)
	if runtime.GOOS == "windows" {
		return strings.EqualFold(a, b)
	}
	return a == b
}

func renameLocal(source, destination string) error {
	if filepath.Clean(source) == filepath.Clean(destination) {
		return nil
	}
	if runtime.GOOS == "windows" && strings.EqualFold(source, destination) {
		temporary := source + ".mem-mv-tmp"
		if err := os.Rename(source, temporary); err != nil {
			return err
		}
		return os.Rename(temporary, destination)
	}
	return os.Rename(source, destination)
}
