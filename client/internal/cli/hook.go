package cli

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"time"

	"github.com/BCCC0/spark-memory/client/internal/config"
	"github.com/BCCC0/spark-memory/client/internal/keys"
	"github.com/BCCC0/spark-memory/client/internal/memerr"
	"github.com/BCCC0/spark-memory/client/internal/pointers"
	"github.com/BCCC0/spark-memory/client/internal/summary"
	"github.com/BCCC0/spark-memory/client/internal/tracking"
	"github.com/BCCC0/spark-memory/client/internal/transport"
)

const markBegin = "# >>> mem auto-track >>>"
const markEnd = "# <<< mem auto-track <<<"

var hookEvents = []string{"pre-commit", "post-commit", "post-merge"}

func hookRepo(o options) (string, error) {
	target := o.flag("repo")
	if target == "" {
		target = "."
	}
	root, err := tracking.Root(target)
	if err != nil {
		return "", memerr.New("invalid_request", target+" is not inside a git repository", map[string]any{"path": target})
	}
	return root, nil
}
func hookDir(repo string) (string, error) {
	gitDirBytes, err := tracking.Git(repo, "rev-parse", "--absolute-git-dir")
	if err != nil {
		return "", err
	}
	hooksPath, err := tracking.Git(repo, "config", "--get", "core.hooksPath")
	if err == nil && strings.TrimSpace(string(hooksPath)) != "" {
		value := strings.TrimSpace(string(hooksPath))
		if filepath.IsAbs(value) {
			return value, nil
		}
		return filepath.Abs(filepath.Join(repo, value))
	}
	return filepath.Join(strings.TrimSpace(string(gitDirBytes)), "hooks"), nil
}
func hookStatus(repo string) map[string]any {
	if repo == "" {
		return map[string]any{"hook_dir": nil, "installed": []string{}}
	}
	dir, _ := hookDir(repo)
	installed := []string{}
	for _, event := range hookEvents {
		data, _ := os.ReadFile(filepath.Join(dir, event))
		if strings.Contains(string(data), markBegin) {
			installed = append(installed, event)
		}
	}
	return map[string]any{"hook_dir": dir, "installed": installed}
}
func shellQuote(value string) string {
	return "'" + strings.ReplaceAll(filepath.ToSlash(value), "'", "'\\''") + "'"
}
func hookBlock(repo, event string) string {
	command := "check"
	if event != "pre-commit" {
		command = "update"
	}
	lines := []string{markBegin, "_mem_bin=${MEM_BIN:-$(command -v mem 2>/dev/null || true)}", "if [ -n \"$_mem_bin\" ]; then"}
	if event == "pre-commit" {
		lines = append(lines, "  \"$_mem_bin\" hook "+command+" --repo "+shellQuote(repo)+" || exit $?")
	} else {
		lines = append(lines, "  \"$_mem_bin\" hook "+command+" --repo "+shellQuote(repo)+" >/dev/null 2>&1 || true")
	}
	if event == "pre-commit" {
		lines = append(lines, "else", "  echo 'mem is not on PATH; staged summary validation did not run' >&2", "  exit 1")
	} else {
		lines = append(lines, "else", "  _mem_gitdir=$(git -C "+shellQuote(repo)+" rev-parse --absolute-git-dir 2>/dev/null || true)", "  if [ -n \"$_mem_gitdir\" ]; then", "    printf '%s\\n' 'mem is not on PATH; commit capture did not run' >> \"$_mem_gitdir/mem-hook.log\"", "  fi", "  unset _mem_gitdir")
	}
	lines = append(lines, "fi", "unset _mem_bin", markEnd)
	return strings.Join(lines, "\n")
}
func stripBlock(text string) string {
	start := strings.Index(text, markBegin)
	if start < 0 {
		return text
	}
	end := strings.Index(text[start:], markEnd)
	if end < 0 {
		return text
	}
	end = start + end + len(markEnd)
	return strings.TrimRight(text[:start], "\n") + "\n" + strings.TrimLeft(text[end:], "\n")
}
func effectivelyEmpty(text string) bool {
	for _, line := range strings.Split(text, "\n") {
		trimmed := strings.TrimSpace(line)
		if trimmed == "" || strings.HasPrefix(trimmed, "#!") {
			continue
		}
		return false
	}
	return true
}

func ensureExecutable(path string) {
	info, err := os.Stat(path)
	if err != nil {
		return
	}
	_ = os.Chmod(path, info.Mode().Perm()|0111)
}

func installHook(repo string, uninstall bool) (map[string]any, error) {
	dir, err := hookDir(repo)
	if err != nil {
		return nil, err
	}
	if !uninstall {
		if err := os.MkdirAll(dir, 0755); err != nil {
			return nil, err
		}
	}
	changed := []string{}
	files := []string{}
	for _, event := range hookEvents {
		path := filepath.Join(dir, event)
		data, err := os.ReadFile(path)
		if err != nil && !os.IsNotExist(err) {
			return nil, err
		}
		if uninstall && !strings.Contains(string(data), markBegin) {
			continue
		}
		body := stripBlock(string(data))
		if uninstall {
			if effectivelyEmpty(body) {
				err = os.Remove(path)
				if err != nil && !os.IsNotExist(err) {
					return nil, err
				}
			} else {
				err = os.WriteFile(path, []byte(body), 0755)
				if err != nil {
					return nil, err
				}
			}
		} else {
			if strings.TrimSpace(body) == "" {
				body = "#!/bin/sh\n"
			}
			body = strings.TrimRight(body, "\n") + "\n" + hookBlock(repo, event) + "\n"
			if err := os.WriteFile(path, []byte(body), 0755); err != nil {
				return nil, err
			}
			ensureExecutable(path)
			files = append(files, path)
		}
		changed = append(changed, event)
	}
	if uninstall {
		return map[string]any{"hook_dir": dir, "removed": changed, "repo": repo}, nil
	}
	return map[string]any{"hook_dir": dir, "installed": changed, "files": files, "repo": repo}, nil
}
func hookCommand(o options) (any, string, error) {
	sub := o.sub
	if sub == "" {
		sub = "ls"
	}
	switch sub {
	case "add", "rm", "ls":
		wd, _ := os.Getwd()
		path, marker, err := config.FindMarker(wd)
		if err != nil {
			return nil, "", err
		}
		if path == "" {
			return nil, "", memerr.New("no_project", "no .mem.toml found in this directory or any parent", map[string]any{"hint": "run `mem init <project>` here"})
		}
		patterns := []string(marker.Track.Patterns)
		if patterns == nil {
			patterns = []string{}
		}
		if sub == "add" {
			if len(o.positional) == 0 {
				return nil, "", memerr.New("usage_error", "hook add requires patterns", nil)
			}
			for _, p := range o.positional {
				found := false
				for _, old := range patterns {
					if old == p {
						found = true
					}
				}
				if !found {
					patterns = append(patterns, p)
				}
			}
		}
		if sub == "rm" {
			if len(o.positional) == 0 {
				return nil, "", memerr.New("usage_error", "hook rm requires patterns", nil)
			}
			var kept []string
			for _, p := range patterns {
				remove := false
				for _, v := range o.positional {
					if p == v {
						remove = true
					}
				}
				if !remove {
					kept = append(kept, p)
				}
			}
			patterns = kept
			if patterns == nil {
				patterns = []string{}
			}
		}
		if sub == "add" || sub == "rm" {
			if err := config.WritePatterns(path, patterns); err != nil {
				return nil, "", err
			}
		}
		repo, _ := tracking.Root(wd)
		status := hookStatus(repo)
		lines := []string{fmt.Sprintf("%d pattern(s) in %s", len(patterns), path)}
		for _, p := range patterns {
			lines = append(lines, "  "+p)
		}
		installed := []string{}
		if values, ok := status["installed"].([]string); ok {
			installed = values
		}
		if len(installed) == 0 {
			lines = append(lines, "hook installed: no")
		} else {
			lines = append(lines, "hook installed: "+strings.Join(installed, ", "))
		}
		return map[string]any{"marker": path, "patterns": patterns, "hook": status}, strings.Join(lines, "\n"), nil
	case "init":
		repo, err := hookRepo(o)
		if err != nil {
			return nil, "", err
		}
		result, err := installHook(repo, o.has("uninstall"))
		verb := "installed into"
		if o.has("uninstall") {
			verb = "uninstalled from"
		}
		return result, verb + " " + stringField(result, "hook_dir"), err
	case "check":
		return hookCheck(o)
	case "update":
		return hookUpdate(o)
	default:
		return nil, "", memerr.New("usage_error", "unknown hook command: "+sub, nil)
	}
}
func markerForRepo(repo string) (string, config.Marker, error) {
	path, marker, err := config.FindMarker(repo)
	if err != nil {
		return "", marker, err
	}
	if path == "" {
		return "", marker, nil
	}
	base := filepath.Dir(path)
	rel, err := filepath.Rel(base, repo)
	if err != nil || strings.HasPrefix(rel, "..") {
		return "", marker, memerr.New("invalid_request", repo+" is outside the project root "+base, nil)
	}
	return path, marker, nil
}
func hookCheck(o options) (any, string, error) {
	repo, err := hookRepo(o)
	if err != nil {
		return nil, "", err
	}
	_, marker, err := markerForRepo(repo)
	if err != nil {
		return nil, "", err
	}
	patterns := []string(marker.Track.Patterns)
	if len(patterns) == 0 {
		return map[string]any{"checked": []string{}, "errors": []any{}}, "no files selected by mem hook", nil
	}
	data, err := tracking.Git(repo, "diff", "--cached", "--name-only", "--diff-filter=ACMRT", "-z")
	if err != nil {
		return nil, "", memerr.New("internal_error", "cannot inspect staged paths", nil)
	}
	selected := []string{}
	problems := []map[string]string{}
	for _, path := range tracking.Selected(patterns, tracking.SplitNUL(data)) {
		if !strings.HasSuffix(strings.ToLower(path), ".md") {
			continue
		}
		selected = append(selected, path)
		blob, err := tracking.Git(repo, "show", ":"+path)
		if err != nil {
			problems = append(problems, map[string]string{"path": path, "message": "cannot read staged file"})
			continue
		}
		if _, err := summary.Validate(string(blob)); err != nil {
			problems = append(problems, map[string]string{"path": path, "message": err.Error()})
		}
	}
	if len(problems) > 0 {
		return nil, "", memerr.New("schema_invalid", "hook-selected Markdown needs a valid summary", map[string]any{"files": problems})
	}
	return map[string]any{"checked": selected, "errors": []any{}}, fmt.Sprintf("validated %d staged Markdown file(s)", len(selected)), nil
}
func hookLog(repo string, messages []string) {
	dir, err := hookDir(repo)
	if err != nil {
		return
	}
	target := filepath.Join(filepath.Dir(dir), "mem-hook.log")
	f, err := os.OpenFile(target, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0600)
	if err != nil {
		return
	}
	defer f.Close()
	for _, msg := range messages {
		fmt.Fprintf(f, "%s %s\n", time.Now().Format("2006-01-02T15:04:05"), msg)
	}
}

// deletedTracked reports tracked paths the revision removed. With a watermark
// the range is since..rev; on the first run it is rev^..rev, matching the
// Python client, so an orphan is not silently missed just because the hook has
// never run in this clone.
func deletedTracked(repo, rev, since string, patterns []string) []string {
	rangeArg := since + ".." + rev
	if since == "" {
		rangeArg = rev + "^.." + rev
	}
	raw, err := tracking.Git(repo, "diff", "--name-only", "--diff-filter=D", "-z", rangeArg)
	if err != nil {
		return []string{}
	}
	return tracking.Selected(patterns, tracking.SplitNUL(raw))
}

func hookUpdate(o options) (any, string, error) {
	repo, err := hookRepo(o)
	if err != nil {
		return nil, "", err
	}
	markerPath, marker, markerErr := markerForRepo(repo)
	patterns := []string(marker.Track.Patterns)
	result := map[string]any{"repo": repo, "patterns": len(patterns), "added": []string{}, "receipts": []string{}, "skipped": []string{}, "orphaned": []string{}, "errors": []string{}, "dry_run": o.has("dry-run")}
	if len(patterns) == 0 {
		return result, "no patterns declared; nothing to do", nil
	}
	problems := []string{}
	addError := func(msg string) {
		problems = append(problems, msg)
		result["errors"] = problems
		hookLog(repo, []string{msg})
	}
	if markerErr != nil {
		// A hook must never fail the commit that triggered it. Containment and
		// marker failures are reported in the result, exactly as Python does
		// for the "outside the project root" case.
		addError(markerErr.Error())
		return result, "", nil
	}
	if markerPath == "" || marker.Project == "" {
		addError("not enrolled in a project here; run `mem init`")
		return result, "", nil
	}
	wd, _ := os.Getwd()
	_, cwdMarker, _ := config.FindMarker(wd)
	if cwdMarker.Project != "" && cwdMarker.Project != marker.Project {
		return nil, "", memerr.New("invalid_request", "current directory belongs to a different project than --repo", nil)
	}
	c, err := config.Load(false, false)
	if err != nil {
		addError(err.Error())
		return result, "", nil
	}
	c.Project = marker.Project
	c.MarkerPath = markerPath
	if marker.Server != "" {
		c.Server = marker.Server
	}
	if c.Credentials == nil {
		addError("not enrolled in a project here; run `mem login`")
		return result, "", nil
	}
	rev := o.flag("rev")
	if rev == "" {
		raw, e := tracking.Git(repo, "rev-parse", "HEAD")
		if e != nil {
			addError("could not resolve a revision to capture")
			return result, "", nil
		}
		rev = strings.TrimSpace(string(raw))
	}
	since := tracking.Watermark(repo)
	var paths []string
	if since == "" {
		raw, e := tracking.Git(repo, "ls-tree", "-r", "-z", "--name-only", rev)
		if e != nil {
			addError("cannot list files at " + rev)
			return result, "", nil
		}
		paths = tracking.SplitNUL(raw)
	} else {
		paths, err = tracking.Paths(repo, rev, since)
		if err != nil {
			addError(err.Error())
			return result, "", nil
		}
	}
	matched := tracking.Selected(patterns, paths)
	result["changed"] = len(paths)
	skipped := []string{}
	for _, p := range paths {
		found := false
		for _, m := range matched {
			if p == m {
				found = true
			}
		}
		if !found {
			skipped = append(skipped, p)
		}
	}
	result["skipped"] = skipped
	deleted := deletedTracked(repo, rev, since, patterns)
	result["orphaned"] = deleted
	active := []string{}
	for _, p := range matched {
		isDeleted := false
		for _, d := range deleted {
			if p == d {
				isDeleted = true
			}
		}
		if !isDeleted {
			active = append(active, p)
		}
	}
	if o.has("dry-run") {
		result["would_add"] = active
		lines := []string{fmt.Sprintf("would capture %d path(s) at %s", len(active), rev[:min(12, len(rev))])}
		for _, p := range active {
			lines = append(lines, "  "+p)
		}
		if len(deleted) > 0 {
			lines = append(lines, "  orphaned: "+strings.Join(deleted, ", "))
		}
		return result, strings.Join(lines, "\n"), nil
	}
	if len(active) > 0 {
		cl, e := client(o, c, 20e9)
		if e != nil {
			addError(e.Error())
			return result, "", nil
		}
		policyRaw, e := req(cl, "GET", "/v1/policy", nil, nil, nil)
		if e != nil {
			addError(strings.Join(active, ", ") + ": " + e.Error())
			return result, "", nil
		}
		policy := object(policyRaw)
		items := []item{}
		for _, p := range active {
			it, e := prepareCapture(repo, rev, p, c, policy)
			if e != nil {
				addError(p + ": " + e.Error())
			} else {
				items = append(items, it)
			}
		}
		if len(problems) == 0 {
			branchBytes, _ := tracking.Git(repo, "rev-parse", "--abbrev-ref", "HEAD")
			branch := strings.TrimSpace(string(branchBytes))
			if branch == "" || branch == "HEAD" {
				branch = "detached"
			}
			message := fmt.Sprintf("sync %d file(s) from %s at %s", len(items), branch, rev[:min(12, len(rev))])
			receipts := []string{}
			added := []string{}
			for start := 0; start < len(items); {
				end, bytes := start, 0
				for end < len(items) && end-start < 1000 && (end == start || bytes+len(items[end].Content) <= 256*1024*1024) {
					bytes += len(items[end].Content)
					end++
				}
				group := items[start:end]
				var response any
				var e error
				if len(group) == 1 {
					it := group[0]
					fields := map[string]string{"project": c.Project, "path": it.ID, "kind": it.Kind, "message": message}
					if it.Origin != "" {
						fields["origin_path"] = it.Origin
					}
					response, e = cl.Multipart("/v1/add", fields, it.Filename, it.Content, headers(keys.Add(digest(it.Content), it.ID, "", it.Origin, message), ""))
				} else {
					uploads := []transport.Upload{}
					for _, it := range group {
						uploads = append(uploads, transport.Upload{Filename: it.Filename, Content: it.Content})
					}
					response, e = cl.MultipartMany(map[string]string{"project": c.Project, "message": message, "items": batchMetadata(group)}, uploads, headers(batchKey(c.Project, message, group), ""))
				}
				if e != nil {
					addError("batch: " + e.Error())
					break
				}
				receipts = append(receipts, stringField(response, "receipt"))
				for _, it := range group {
					added = append(added, it.Path)
				}
				start = end
			}
			result["receipts"] = receipts
			result["added"] = added
		}
	}
	if len(problems) == 0 {
		if err := tracking.SetWatermark(repo, rev); err != nil {
			addError(err.Error())
		}
	}
	result["status"] = "idle"
	if lenSliceString(result["receipts"]) > 0 {
		result["status"] = "queued"
	}
	return result, fmt.Sprintf("queued %d path(s) from %s", lenSliceString(result["added"]), rev[:min(12, len(rev))]), nil
}
func lenSliceString(value any) int {
	if rows, ok := value.([]string); ok {
		return len(rows)
	}
	return 0
}
func prepareCapture(repo, rev, path string, c config.Config, policy map[string]any) (item, error) {
	var it item
	working := filepath.Join(repo, filepath.FromSlash(path))
	id, err := derive(working, filepath.Dir(c.MarkerPath), "")
	if err != nil {
		return it, err
	}
	it.ID, it.Filename, it.Path, it.Origin = id, filepath.Base(working), path, working
	ext := strings.ToLower(filepath.Ext(working))
	allowed := false
	if values, ok := policy["inline_extensions"].([]any); ok {
		for _, v := range values {
			if str(v) == ext {
				allowed = true
			}
		}
	}
	if allowed {
		content, err := tracking.Git(repo, "show", rev+":"+path)
		if err != nil {
			return it, err
		}
		it.Content = content
		it.Kind = "text"
		if ext == ".md" {
			it.Kind = "note"
			if _, err := summary.Validate(string(content)); err != nil {
				return it, err
			}
		}
		if max, ok := policy["max_upload_bytes"].(float64); ok && len(content) > int(max) {
			return it, memerr.New("invalid_request", "inline limit exceeded", nil)
		}
	} else {
		if c.Credentials == nil || c.Credentials.Alias == "" {
			return it, memerr.New("unauthorized", "device alias required", nil)
		}
		p, err := pointers.Parse("@file("+c.Credentials.Alias+":"+working+")", false, "")
		if err != nil {
			return it, err
		}
		it.Content = []byte(p.Canonical() + "\n")
		it.Kind = "pointer"
		it.Origin = ""
	}
	it.SHA = digest(it.Content)
	return it, nil
}
func batchKey(project, message string, items []item) string {
	h := sha256.New()
	h.Write([]byte(project))
	h.Write([]byte(message))
	for _, it := range items {
		for _, s := range []string{it.ID, it.Kind, it.Filename, it.Origin, it.SHA} {
			h.Write([]byte(s))
			h.Write([]byte{0})
		}
	}
	return hex.EncodeToString(h.Sum(nil)) + ":add-many"
}
func batchMetadata(items []item) string {
	rows := []map[string]any{}
	for _, it := range items {
		rows = append(rows, map[string]any{"id": it.ID, "filename": it.Filename, "kind": it.Kind, "origin_path": emptyToNil(it.Origin)})
	}
	data, _ := json.Marshal(rows)
	return string(data)
}
