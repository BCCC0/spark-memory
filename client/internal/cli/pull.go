package cli

import (
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"

	"github.com/BCCC0/spark-memory/client/internal/config"
	"github.com/BCCC0/spark-memory/client/internal/ids"
	"github.com/BCCC0/spark-memory/client/internal/memerr"
	"github.com/BCCC0/spark-memory/client/internal/pointers"
	"github.com/BCCC0/spark-memory/client/internal/transport"
)

func rawPath(project, noteID string) string {
	return "/raw/" + url.PathEscape(project) + "/" + escapeID(noteID)
}

func noteRows(cl *transport.Client, project string) ([]any, error) {
	rows := []any{}
	cursor := ""
	for {
		q := map[string]string{"project": project, "full": "true", "limit": "500"}
		if cursor != "" {
			q["cursor"] = cursor
		}
		raw, err := req(cl, "GET", "/v1/notes", q, nil, nil)
		if err != nil {
			return nil, err
		}
		page := object(raw)
		if notes, ok := page["notes"].([]any); ok {
			rows = append(rows, notes...)
		}
		cursor = stringField(page, "next_cursor")
		if cursor == "" {
			return rows, nil
		}
	}
}

func atomicWrite(path string, data []byte) error {
	if err := os.MkdirAll(filepath.Dir(path), 0755); err != nil {
		return err
	}
	file, err := os.CreateTemp(filepath.Dir(path), ".mem-pull-*")
	if err != nil {
		return err
	}
	temporary := file.Name()
	defer os.Remove(temporary)
	if _, err := file.Write(data); err != nil {
		file.Close()
		return err
	}
	if err := file.Sync(); err != nil {
		file.Close()
		return err
	}
	if err := file.Close(); err != nil {
		return err
	}
	_ = os.Chmod(temporary, 0644)
	if err := os.Link(temporary, path); err != nil {
		if os.IsExist(err) {
			return memerr.New("local_path_exists", "destination already exists: "+path, map[string]any{"path": path})
		}
		return err
	}
	return nil
}

func resolveForDestination(path string) string {
	if resolved, err := filepath.EvalSymlinks(path); err == nil {
		return resolved
	}
	parent := filepath.Dir(path)
	if parent == path {
		return path
	}
	return filepath.Join(resolveForDestination(parent), filepath.Base(path))
}

func pullTarget(path, root string) (string, string) {
	abs, err := filepath.Abs(path)
	if err != nil {
		return "", "missing_origin"
	}
	parent, err := filepath.EvalSymlinks(filepath.Dir(abs))
	if err != nil {
		return "", "missing_parent"
	}
	resolved, err := requireOriginInside(parent, root)
	if err != nil {
		return "", "outside_root"
	}
	if marker := nearestMarker(resolved); marker != "" && !samePath(marker, filepath.Join(root, ".mem.toml")) {
		return "", "nested_project"
	}
	return filepath.Join(resolved, filepath.Base(abs)), ""
}

func pullCommand(o options, c config.Config, cl *transport.Client) (any, string, error) {
	root := filepath.Dir(c.MarkerPath)
	own := ""
	if c.Credentials != nil {
		own = c.Credentials.DeviceID
	}
	rows, err := noteRows(cl, c.Project)
	if err != nil {
		return nil, "", err
	}
	requested := map[string]bool{}
	for _, id := range o.positional {
		requested[id] = true
	}
	seen := map[string]bool{}
	written := []string{}
	skipped := []string{}
	damaged := []map[string]any{}

	for _, raw := range rows {
		row := object(raw)
		noteID := stringField(row, "id")
		if len(requested) > 0 && !requested[noteID] {
			continue
		}
		kind := stringField(row, "kind")
		if kind != "note" && kind != "text" {
			continue
		}
		seen[noteID] = true
		origin := object(row["origin"])
		pointer := stringField(origin, "pointer")
		originPath := stringField(origin, "path")
		if len(requested) == 0 && stringField(origin, "device") != own {
			continue
		}
		if stringField(origin, "device") != own || (pointer == "" && originPath == "") {
			damaged = append(damaged, map[string]any{"id": noteID, "reason": "missing_origin"})
			continue
		}
		if originPath == "" {
			parsed, err := pointers.Parse(pointer, false, "")
			if err != nil {
				damaged = append(damaged, map[string]any{"id": noteID, "reason": "missing_origin"})
				continue
			}
			originPath = parsed.Path
		}
		target, reason := pullTarget(originPath, root)
		if reason != "" {
			damaged = append(damaged, map[string]any{"id": noteID, "path": originPath, "reason": reason})
			continue
		}
		if info, err := os.Stat(target); err == nil {
			if info.Mode().IsRegular() {
				skipped = append(skipped, target)
			} else {
				damaged = append(damaged, map[string]any{"id": noteID, "path": target, "reason": "not_a_file"})
			}
			continue
		} else if _, lerr := os.Lstat(target); lerr == nil {
			// Broken symlink or another unreadable entry: do not replace it.
			damaged = append(damaged, map[string]any{"id": noteID, "path": target, "reason": "not_a_file"})
			continue
		}
		data, err := cl.Raw(rawPath(c.Project, noteID))
		if err != nil {
			return nil, "", err
		}
		if err := atomicWrite(target, data); err != nil {
			damaged = append(damaged, map[string]any{"id": noteID, "path": target, "reason": "write_failed: " + err.Error()})
			continue
		}
		written = append(written, target)
	}
	for id := range requested {
		if !seen[id] {
			damaged = append(damaged, map[string]any{"id": id, "reason": "not_found"})
		}
	}

	result := map[string]any{"written": written, "skipped": skipped, "damaged": damaged}
	human := fmt.Sprintf("pulled %d file(s); skipped %d", len(written), len(skipped))
	if len(damaged) > 0 {
		human += fmt.Sprintf("; damaged %d", len(damaged))
	}
	if len(damaged) > 0 {
		return result, human, memerr.New("invalid_request", "pull completed with damaged files", map[string]any{"damaged": damaged})
	}
	return result, human, nil
}

func cleanExportDestination(value string) (string, error) {
	if strings.TrimSpace(value) == "" {
		return "", memerr.New("usage_error", "export requires --dst", nil)
	}
	abs, err := filepath.Abs(value)
	if err != nil {
		return "", err
	}
	abs = resolveForDestination(abs)
	if marker := nearestMarker(abs); marker != "" {
		return "", memerr.New("invalid_request", "export destination is inside a memory project", map[string]any{"destination": abs, "marker": marker})
	}
	if info, err := os.Stat(abs); err == nil {
		if !info.IsDir() {
			return "", memerr.New("invalid_request", "export destination is not a directory", map[string]any{"destination": abs})
		}
		entries, err := os.ReadDir(abs)
		if err != nil {
			return "", memerr.New("internal_error", err.Error(), nil)
		}
		if len(entries) > 0 {
			return "", memerr.New("invalid_request", "export destination is not empty", map[string]any{"destination": abs})
		}
	}
	return abs, nil
}

func exportCommand(o options, c config.Config, cl *transport.Client) (any, string, error) {
	project, err := ids.Project(o.positional[0])
	if err != nil {
		return nil, "", err
	}
	destination, err := cleanExportDestination(o.flag("dst"))
	if err != nil {
		return nil, "", err
	}
	rows, err := noteRows(cl, project)
	if err != nil {
		return nil, "", err
	}
	written := []string{}
	for _, raw := range rows {
		row := object(raw)
		kind := stringField(row, "kind")
		if kind != "note" && kind != "text" {
			continue
		}
		noteID, err := ids.Validate(stringField(row, "id"))
		if err != nil {
			return nil, "", err
		}
		relative := noteID
		if kind == "note" {
			relative += ".md"
		}
		target := filepath.Join(destination, filepath.FromSlash(relative))
		if _, err := os.Lstat(target); err == nil {
			return nil, "", memerr.New("local_path_exists", "export target already exists: "+target, map[string]any{"path": target})
		}
		data, err := cl.Raw(rawPath(project, noteID))
		if err != nil {
			return nil, "", err
		}
		if err := atomicWrite(target, data); err != nil {
			return nil, "", memerr.New("internal_error", err.Error(), map[string]any{"path": target})
		}
		written = append(written, target)
	}
	result := map[string]any{"project": project, "destination": destination, "count": len(written), "written": written}
	return result, fmt.Sprintf("exported %d file(s) to %s", len(written), destination), nil
}
