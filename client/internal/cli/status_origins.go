package cli

import (
	"fmt"
	"os"
	"path/filepath"
	"runtime"
	"strings"

	"github.com/BCCC0/spark-memory/client/internal/config"
	"github.com/BCCC0/spark-memory/client/internal/pointers"
	"github.com/BCCC0/spark-memory/client/internal/transport"
)

func auditOrigins(c config.Config, cl *transport.Client) (map[string]any, error) {
	root := filepath.Dir(c.MarkerPath)
	own := ""
	if c.Credentials != nil {
		own = c.Credentials.DeviceID
	}
	rows := []any{}
	cursor := ""
	for {
		q := map[string]string{"project": c.Project, "full": "true", "limit": "500"}
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
			break
		}
	}

	entries := []map[string]any{}
	seen := map[string]int{}
	for _, raw := range rows {
		row := object(raw)
		switch stringField(row, "kind") {
		case "topic", "file", "dir", "locked":
			// Topics and pointers have no note origin.
			continue
		}
		origin := object(row["origin"])
		if stringField(origin, "device") != own {
			entries = append(entries, map[string]any{"id": stringField(row, "id"), "state": "remote"})
			continue
		}
		pointer := stringField(origin, "pointer")
		path := stringField(origin, "path")
		if pointer == "" && path == "" {
			entries = append(entries, map[string]any{"id": stringField(row, "id"), "state": "unresolved"})
			continue
		}
		if path == "" {
			parsed, err := pointers.Parse(pointer, false, "")
			if err != nil {
				entries = append(entries, map[string]any{"id": stringField(row, "id"), "state": "unresolved"})
				continue
			}
			path = parsed.Path
		}
		key := path
		if runtime.GOOS == "windows" {
			key = strings.ToLower(path)
		}
		seen[key]++
		state := "ok"
		if _, err := os.Stat(path); err != nil {
			state = "missing"
		} else if _, err := requireOriginInside(path, root); err != nil {
			state = "outside_root"
		} else if marker := nearestMarker(filepath.Dir(path)); marker != "" && !samePath(marker, filepath.Join(root, ".mem.toml")) {
			state = "cross_project"
		}
		entries = append(entries, map[string]any{"id": stringField(row, "id"), "path": path, "state": state})
	}

	counts := map[string]int{"ok": 0, "missing": 0, "outside_root": 0, "cross_project": 0,
		"ambiguous": 0, "remote": 0, "unresolved": 0}
	for _, entry := range entries {
		if path := stringField(entry, "path"); path != "" {
			key := path
			if runtime.GOOS == "windows" {
				key = strings.ToLower(path)
			}
			if seen[key] > 1 {
				entry["state"] = "ambiguous"
			}
		}
		counts[str(entry["state"])]++
	}
	return map[string]any{"counts": counts, "entries": entries}, nil
}

func originsHuman(value map[string]any) string {
	if counts, ok := value["counts"].(map[string]int); ok {
		parts := []string{}
		for _, name := range []string{"ok", "missing", "outside_root", "cross_project", "ambiguous", "remote", "unresolved"} {
			parts = append(parts, fmt.Sprintf("%s %d", name, counts[name]))
		}
		return strings.Join(parts, ", ")
	}
	counts := object(value["counts"])
	parts := []string{}
	for _, name := range []string{"ok", "missing", "outside_root", "cross_project", "ambiguous", "remote", "unresolved"} {
		parts = append(parts, name+" "+str(counts[name]))
	}
	return strings.Join(parts, ", ")
}
