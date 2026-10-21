package cli

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/BCCC0/spark-memory/client/internal/config"
	"github.com/BCCC0/spark-memory/client/internal/ids"
	"github.com/BCCC0/spark-memory/client/internal/memerr"
	"github.com/BCCC0/spark-memory/client/internal/paths"
	"github.com/BCCC0/spark-memory/client/internal/pointers"
	"github.com/BCCC0/spark-memory/client/internal/pyrepr"
	"github.com/BCCC0/spark-memory/client/internal/transport"
)

func readCommand(o options) (any, string, error) {
	requireProject := map[string]bool{"manifest": true, "ls": true, "read": true, "pull": true}
	if o.command == "status" && o.has("origins") {
		requireProject["status"] = true
	}
	if o.command == "search" && o.flag("project") == "" {
		requireProject["search"] = true
	}
	timeout := 20 * time.Second
	if o.command == "flush" || (o.command == "search" && o.flag("mode") == "hybrid") || (o.command == "index" && o.has("rebuild")) {
		timeout = 180 * time.Second
	}
	c, cl, err := setup(o, requireProject[o.command], timeout)
	if err != nil {
		return nil, "", err
	}
	switch o.command {
	case "status":
		result, err := req(cl, "GET", "/v1/status", map[string]string{"project": c.Project}, nil, nil)
		r := object(result)
		project := stringField(r, "project")
		if project == "" {
			project = "(none)"
		}
		message := stringField(r, "message")
		if message == "" {
			message = "(none)"
		}
		lock := object(r["lock"])
		lockText := "free"
		if lock["held"] == true {
			lockText = "held by " + str(lock["by"])
		}
		tree := "clean"
		if r["index_dirty"] == true {
			tree = "DIRTY -- uncommitted content present"
		}
		human := fmt.Sprintf("server   %s\ncommit   %s\nproject  %s\nhead     %s\nmessage  %s\nlock     %s\ntree     %s\nmax_bytes %s", cl.Base, str(r["commit"]), project, str(r["head"]), message, lockText, tree, str(object(r["read"])["max_bytes"]))
		if o.has("origins") {
			origins, auditErr := auditOrigins(c, cl)
			if auditErr != nil {
				return nil, "", auditErr
			}
			r["origins"] = origins
			human += "\norigins  " + originsHuman(origins)
		}
		return result, human, err
	case "flush":
		return flush(cl)
	case "pull":
		return pullCommand(o, c, cl)
	case "export":
		return exportCommand(o, c, cl)
	case "projects":
		result, err := req(cl, "GET", "/v1/projects", nil, nil, nil)
		var lines []string
		if rows, ok := result.([]any); ok {
			for _, row := range rows {
				r := object(row)
				lines = append(lines, fmt.Sprintf("%-24s %4s notes  %s", str(r["project"]), str(r["notes"]), str(r["updated"])))
			}
		}
		if len(lines) == 0 {
			lines = []string{"(no projects)"}
		}
		return result, strings.Join(lines, "\n"), err
	case "manifest":
		if err := o.count(0, 1); err != nil {
			return nil, "", err
		}
		topic := ""
		if len(o.positional) == 1 && o.positional[0] != "" {
			topic, err = ids.Validate(o.positional[0])
			if err != nil {
				return nil, "", err
			}
		}
		result, err := req(cl, "GET", "/v1/manifest", map[string]string{"project": c.Project, "topic": topic}, nil, nil)
		return result, fmt.Sprintf("%d entries at revision %s", lenSlice(object(result)["entries"]), stringField(result, "revision")), err
	case "ls":
		if err := o.count(0, 1); err != nil {
			return nil, "", err
		}
		limit, err := positiveInt(o.flag("limit"), 50)
		if err != nil {
			return nil, "", err
		}
		q := map[string]string{"project": c.Project, "status": o.flag("status"), "type": o.flag("type"), "tag": o.flag("tag"), "since": o.flag("since"), "sort": o.flag("sort"), "limit": fmt.Sprint(limit), "cursor": o.flag("cursor")}
		if q["sort"] == "" {
			q["sort"] = "updated"
		}
		if len(o.positional) == 1 {
			q["prefix"] = o.positional[0]
		}
		if o.has("full") {
			q["full"] = "True"
		} else {
			q["full"] = "False"
		}
		result, err := req(cl, "GET", "/v1/notes", q, nil, nil)
		var lines []string
		if rows, ok := object(result)["notes"].([]any); ok {
			for _, row := range rows {
				r := object(row)
				lines = append(lines, fmt.Sprintf("%-44s %-10s %-9s %s", str(r["id"]), str(r["status"]), str(r["type"]), str(r["summary"])))
			}
		}
		if cursor := stringField(result, "next_cursor"); cursor != "" {
			lines = append(lines, "-- next: "+cursor)
		}
		if len(lines) == 0 {
			lines = []string{"(no notes)"}
		}
		return result, strings.Join(lines, "\n"), err
	case "read":
		if err := o.count(1, -1); err != nil {
			return nil, "", err
		}
		if len(o.positional) == 1 {
			return readOne(o, c, cl, o.positional[0])
		}
		results := []any{}
		humans := []string{}
		var first *memerr.Error
		failed := []string{}
		for _, raw := range o.positional {
			result, human, err := readOne(o, c, cl, raw)
			if err != nil {
				var me *memerr.Error
				if !errors.As(err, &me) {
					me = memerr.New("internal_error", err.Error(), nil)
				}
				if first == nil {
					first = me
				}
				results = append(results, map[string]any{"id": raw, "ok": false, "error": me})
				humans = append(humans, raw+": "+me.Error())
				failed = append(failed, raw)
			} else {
				results = append(results, map[string]any{"id": raw, "ok": true, "record": result})
				humans = append(humans, raw+":\n"+human)
			}
		}
		if first != nil {
			e := memerr.New(first.Code, "one or more reads failed", map[string]any{"failed": failed})
			return map[string]any{"results": results}, strings.Join(humans, "\n\n"), e
		}
		return map[string]any{"results": results}, strings.Join(humans, "\n\n"), nil
	case "search":
		word, err := o.one()
		if err != nil {
			return nil, "", err
		}
		project := o.flag("project")
		if project == "" {
			project = c.Project
		}
		mode := o.flag("mode")
		if mode == "" {
			mode = "text"
		}
		if mode != "text" && mode != "hybrid" {
			return nil, "", memerr.New("usage_error", "invalid search mode", nil)
		}
		limit, err := positiveInt(o.flag("limit"), 20)
		if err != nil {
			return nil, "", err
		}
		result, err := req(cl, "GET", "/v1/search", map[string]string{"project": project, "q": word, "mode": mode, "status": o.flag("status"), "type": o.flag("type"), "tag": o.flag("tag"), "limit": fmt.Sprint(limit)}, nil, nil)
		var lines []string
		if hits, ok := object(result)["hits"].([]any); ok {
			for _, hit := range hits {
				r := object(hit)
				suffix := ""
				if rangeValue, ok := r["line_range"].([]any); ok && len(rangeValue) > 0 {
					suffix = ":" + str(rangeValue[0])
				}
				via := stringField(result, "mode")
				if matched, ok := r["matched_by"].([]any); ok && len(matched) > 0 {
					names := []string{}
					for _, name := range matched {
						names = append(names, str(name))
					}
					via = strings.Join(names, ",")
				}
				lines = append(lines, fmt.Sprintf("%s%-6s [%-11s] %s", str(r["id"]), suffix, via, stringField(r, "summary")))
			}
		}
		if pending, ok := object(result)["queue_pending"].(float64); ok && pending > 0 {
			lines = append(lines, fmt.Sprintf("warning: %s write(s) pending; results may be stale", str(pending)))
		}
		if object(result)["index_lag"] == true {
			lines = append(lines, "warning: search index is behind the committed revision")
		}
		if len(lines) == 0 {
			lines = []string{"(no hits)"}
		}
		return result, strings.Join(lines, "\n"), err
	case "index":
		if o.has("rebuild") {
			result, err := req(cl, "POST", "/v1/index/rebuild", nil, nil, nil)
			return result, fmt.Sprintf("rebuilt: %s chunks over %s notes, %s embedded", stringField(result, "chunks"), stringField(result, "notes"), stringField(result, "embedded")), err
		}
		result, err := req(cl, "GET", "/v1/index/status", nil, nil, nil)
		textIndex := object(object(result)["text"])
		hybrid := object(object(result)["hybrid"])
		lines := []string{fmt.Sprintf("kind        %s", stringField(result, "kind")), fmt.Sprintf("head        %s", stringField(result, "head")), fmt.Sprintf("text index  head=%s age=%s", str(textIndex["indexed_head"]), str(textIndex["age"]))}
		if hybrid["available"] == true {
			lines = append(lines, fmt.Sprintf("semantic    model=%s chunks=%s notes=%s", str(hybrid["model"]), str(hybrid["chunks"]), str(hybrid["notes"])), fmt.Sprintf("            indexed_head=%s", str(hybrid["indexed_head"])))
		} else {
			reason := stringField(hybrid, "reason")
			if reason == "" {
				reason = "not installed"
			}
			lines = append(lines, "semantic    unavailable ("+reason+")")
		}
		return result, strings.Join(lines, "\n"), err
	case "devices":
		if id := o.flag("retire"); id != "" {
			result, err := req(cl, "POST", "/v1/devices/retire", nil, map[string]any{"device_id": id}, nil)
			return result, "retired " + stringField(result, "retired") + " (the record is kept for provenance)", err
		}
		result, err := req(cl, "GET", "/v1/devices", nil, nil, nil)
		lines := []string{}
		if rows, ok := result.([]any); ok {
			for _, row := range rows {
				r := object(row)
				state := "active"
				if r["retired_at"] != nil {
					state = "retired"
				}
				last := stringField(r, "last_seen_ip")
				if last == "" {
					last = "-"
				}
				lines = append(lines, fmt.Sprintf("%-26s %-18s %-8s last=%s", stringField(r, "device_id"), stringField(r, "display_name"), state, last))
			}
		}
		if len(lines) == 0 {
			lines = []string{"(no devices)"}
		}
		return result, strings.Join(lines, "\n"), err
	}
	return nil, "", memerr.New("usage_error", "unknown read command", nil)
}
func lenSlice(value any) int {
	if v, ok := value.([]any); ok {
		return len(v)
	}
	return 0
}

var flushTimeout = 3 * time.Minute

func flush(cl *transport.Client) (any, string, error) {
	result, err := req(cl, "GET", "/v1/queue", nil, nil, nil)
	if err != nil {
		return nil, "", err
	}
	through := object(result)["through"]
	deadline := time.Now().Add(flushTimeout)
	for object(result)["pending"] != float64(0) {
		if time.Now().After(deadline) {
			return nil, "", memerr.New("lock_timeout", "flush timed out waiting for queued writes and indexing", map[string]any{"through": through})
		}
		time.Sleep(100 * time.Millisecond)
		result, err = req(cl, "GET", "/v1/queue", map[string]string{"through": str(through)}, nil, nil)
		if err != nil {
			return nil, "", err
		}
	}
	if failures := lenSlice(object(result)["failed"]); failures > 0 {
		return nil, "", memerr.New("queued_write_failed", fmt.Sprintf("%d queued write(s) failed", failures), map[string]any{"through": through, "failed": object(result)["failed"]})
	}
	return map[string]any{"status": "flushed", "through": through, "head": object(result)["head"]}, fmt.Sprintf("flushed through job %s", str(through)), nil
}

// escapeID keeps "/" as the path separator, matching Python's urllib.quote.
func escapeID(id string) string {
	parts := strings.Split(id, "/")
	for i, part := range parts {
		parts[i] = url.PathEscape(part)
	}
	return strings.Join(parts, "/")
}

func splitID(raw, explicit string) (string, string, string, string, error) {
	section, span := "", ""
	if head, tail, ok := strings.Cut(raw, "#"); ok {
		raw, section = head, strings.TrimSpace(tail)
	} else if head, found, ok := strings.Cut(raw, ":"); ok {
		raw = head
		start, end, separated := strings.Cut(found, "-")
		first, firstErr := strconv.Atoi(strings.TrimSpace(start))
		last, lastErr := strconv.Atoi(strings.TrimSpace(end))
		if !separated || firstErr != nil || lastErr != nil {
			return "", "", "", "", memerr.New("usage_error", "range "+pyrepr.String(found)+" is not <start>-<end>", map[string]any{"range": found})
		}
		span = fmt.Sprintf("%d-%d", first, last)
	}
	revision := explicit
	if index := strings.LastIndex(raw, "@"); index > 0 && index < len(raw)-1 {
		head, tail := raw[:index], raw[index+1:]
		raw = head
		if revision == "" {
			revision = tail
		}
	}
	id, err := ids.Validate(raw)
	return id, revision, section, span, err
}
func readOne(o options, c config.Config, cl *transport.Client, raw string) (any, string, error) {
	id, rev, section, span, err := splitID(raw, o.flag("revision"))
	if err != nil {
		return nil, "", err
	}
	linkOnly := o.has("link-only")
	if linkOnly && (section != "" || span != "" || strings.Contains(raw, "#") || strings.Contains(raw, ":")) {
		return nil, "", memerr.New("usage_error", "--link-only applies to whole notes; a section or range always inlines", map[string]any{"id": id})
	}
	if o.has("outline") && linkOnly {
		return nil, "", memerr.New("usage_error", "--link-only does not apply to --outline; an outline has no body", map[string]any{"id": id})
	}
	if o.has("outline") && (strings.Contains(raw, "#") || strings.Contains(raw, ":")) {
		return nil, "", memerr.New("usage_error", "--outline returns headings only; a section or range does not apply", map[string]any{"id": id})
	}
	path := "/v1/notes/" + escapeID(id)
	q := map[string]string{"project": c.Project, "revision": rev, "section": section, "range": span, "max_bytes": fmt.Sprint(c.MaxBytes)}
	if linkOnly {
		q["link_only"] = "true"
	}
	if o.has("outline") {
		path = "/v1/outline"
		q = map[string]string{"project": c.Project, "id": id, "revision": rev}
	}
	result, err := req(cl, "GET", path, q, nil, http.Header(nil))
	if err != nil {
		return nil, "", err
	}
	r := object(result)
	if r["status"] == "pending" {
		if o.has("outline") {
			return result, fmt.Sprintf("%s: pending as %s", id, str(r["receipt"])), nil
		}
		committed := stringField(r, "committed_revision")
		if committed == "" {
			committed = "none"
		}
		return result, fmt.Sprintf("%s: pending as %s (committed: %s)", id, str(r["receipt"]), committed), nil
	}
	if r["status"] == "failed" {
		failure := object(r["error"])
		detail := object(failure["detail"])
		detail["receipt"] = r["receipt"]
		detail["committed_revision"] = r["committed_revision"]
		return nil, "", memerr.New(str(failure["code"]), str(failure["message"]), detail)
	}
	if kind := stringField(r, "kind"); kind == "file" || kind == "dir" || kind == "locked" {
		if c.Credentials != nil && c.Credentials.Alias == stringField(r, "points_at") {
			local := stringField(r, "local_path")
			if kind != "locked" {
				p, err := pointers.Parse(stringField(r, "pointer"), false, "")
				if err != nil {
					return nil, "", err
				}
				local = p.Path
				r["local_path"] = local
			}
			if local != "" {
				target := paths.ExpandUser(local)
				if kind == "locked" {
					r["local_resolvable"] = true
				} else if info, err := os.Stat(target); err == nil && ((kind == "file" && info.Mode().IsRegular()) || (kind == "dir" && info.IsDir())) {
					r["local_resolvable"] = true
					r["local_size"] = info.Size()
					r["local_mtime"] = float64(info.ModTime().UnixNano()) / 1e9
				} else {
					r["local_resolvable"] = false
				}
			}
		}
	}
	if o.has("outline") {
		var lines []string
		if headings, ok := r["headings"].([]any); ok {
			for _, h := range headings {
				row := object(h)
				level := int(row["level"].(float64))
				lines = append(lines, strings.Repeat("  ", level-1)+str(row["text"]))
			}
		}
		return result, strings.Join(lines, "\n"), nil
	}
	if body, ok := r["body"].(string); ok && len([]byte(body)) > c.MaxBytes && section == "" && span == "" {
		delete(r, "body")
		r["truncated"] = true
		r["deferred_by_client"] = true
	}
	if r["truncated"] == true && r["link_only"] != true {
		if size, ok := r["bytes"].(float64); ok && size > float64(c.MaxBytes) {
			r["deferred_by_client"] = true
		}
	}
	kind := stringField(r, "kind")
	if kind != "file" && kind != "dir" && kind != "locked" {
		annotateFreshness(r)
		stripFreshnessHashes(r)
	}
	if kind == "file" || kind == "dir" || kind == "locked" {
		state := "resolvable on this device"
		if r["local_resolvable"] != true {
			device := stringField(r, "points_at")
			if device == "" {
				device = "another device"
			}
			state = "points at " + device + "; availability unverified"
		}
		return r, fmt.Sprintf("%s is a %s pointer (%s); target bytes are not served", id, kind, state), nil
	}
	if body, ok := r["body"].(string); ok {
		return r, body, nil
	}
	return r, fmt.Sprintf("%s is %s bytes; %s", id, str(r["bytes"]), freshnessLine(r)), nil
}
func freshnessLine(r map[string]any) string {
	line := "fetch: " + stringField(r, "fetch_url")
	switch stringField(r, "source") {
	case "local":
		line = "working file is newer: " + stringField(r, "origin_path")
	case "same":
		line = "working file matches the committed revision: " + stringField(r, "origin_path")
	case "server":
		if stringField(r, "origin_path") != "" {
			line = "committed revision is newer than your working file (" + stringField(r, "origin_path") + "); fetch: " + stringField(r, "fetch_url")
		}
	}
	if r["link_only"] == true && !strings.Contains(line, "fetch: ") && stringField(r, "fetch_url") != "" {
		line += "; link: " + stringField(r, "fetch_url")
	}
	return line
}
func annotateFreshness(r map[string]any) {
	origin := stringField(r, "origin_path")
	if origin == "" {
		// The field is always present for a non-pointer read: without it a
		// `source` value would be ambiguous (the skill tells callers to read
		// `local_file` first).
		r["source"] = "none"
		r["local_file"] = false
		return
	}
	working := paths.Resolve(origin)
	if working == "" {
		delete(r, "origin_path")
		r["source"] = "none"
		r["local_file"] = false
		return
	}
	data, err := os.ReadFile(working)
	if err != nil {
		delete(r, "origin_path")
		r["source"] = "none"
		r["local_file"] = false
		return
	}
	info, err := os.Stat(working)
	if err != nil {
		delete(r, "origin_path")
		r["source"] = "none"
		r["local_file"] = false
		return
	}
	body := bodyOnly(string(data))
	body = strings.ReplaceAll(body, "\r\n", "\n")
	body = strings.ReplaceAll(body, "\r", "\n")
	body = strings.TrimRight(body, "\n")
	bodyHash := sha256.Sum256([]byte(body))
	r["origin_path"] = working
	r["local_file"] = true
	r["local_bytes"] = info.Size()
	r["local_mtime"] = float64(info.ModTime().UnixNano()) / 1e9
	committed := stringField(r, "body_sha256")
	if committed == "" {
		r["source"] = "none"
		return
	}
	if committed == hex.EncodeToString(bodyHash[:]) {
		r["source"] = "same"
		r["body_matches"] = true
		return
	}
	r["body_matches"] = false
	r["source"] = "server"
	if stamp := stringField(r, "note_updated"); stamp != "" {
		if when, err := time.Parse(time.RFC3339Nano, stamp); err == nil {
			if info.ModTime().After(when) {
				r["source"] = "local"
			}
			r["timestamp_basis"] = "mtime_vs_commit_time"
		}
	}
}

// stripFreshnessHashes removes the inputs to the local comparison. `sha256`
// stays: it identifies the committed file and is what `--expect` takes. The
// body and local hashes are implementation detail, not part of the CLI contract.
func stripFreshnessHashes(r map[string]any) {
	delete(r, "body_sha256")
	delete(r, "content_sha256")
	delete(r, "local_sha256")
	delete(r, "local_body_sha256")
}
func bodyOnly(text string) string {
	text = strings.ReplaceAll(text, "\r\n", "\n")
	text = strings.ReplaceAll(text, "\r", "\n")
	lines := strings.Split(text, "\n")
	if len(lines) == 0 || strings.TrimSpace(lines[0]) != "---" {
		return text
	}
	for i := 1; i < len(lines); i++ {
		if strings.TrimSpace(lines[i]) == "---" {
			return strings.TrimLeft(strings.Join(lines[i+1:], "\n"), "\n")
		}
	}
	return text
}
