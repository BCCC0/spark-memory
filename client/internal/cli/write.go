package cli

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/BCCC0/spark-memory/client/internal/config"
	"github.com/BCCC0/spark-memory/client/internal/devicehome"
	"github.com/BCCC0/spark-memory/client/internal/ids"
	"github.com/BCCC0/spark-memory/client/internal/keys"
	"github.com/BCCC0/spark-memory/client/internal/memerr"
	"github.com/BCCC0/spark-memory/client/internal/pointers"
	"github.com/BCCC0/spark-memory/client/internal/summary"
	"github.com/BCCC0/spark-memory/client/internal/transport"
)

func digest(data []byte) string { sum := sha256.Sum256(data); return hex.EncodeToString(sum[:]) }
func headers(key, expect string) http.Header {
	h := http.Header{}
	if key != "" {
		h.Set("Idempotency-Key", key)
	}
	if expect != "" {
		h.Set("If-Match", expect)
	}
	return h
}
func derive(path, base, as string) (string, error) {
	if as != "" {
		return ids.Validate(as)
	}
	abs, err := filepath.Abs(path)
	if err != nil {
		return "", err
	}
	rel, err := filepath.Rel(base, abs)
	if err != nil || strings.HasPrefix(rel, "..") || filepath.IsAbs(rel) {
		return "", memerr.New("invalid_request", fmt.Sprintf("%s is outside the project root %s", path, base), map[string]any{"path": path, "root": base, "hint": "pass --as <id>"})
	}
	return ids.Validate(ids.FromPath(rel))
}
func writeCommand(o options) (any, string, error) {
	c, cl, err := setup(o, true, 20e9)
	if err != nil {
		return nil, "", err
	}
	switch o.command {
	case "rm":
		arg, err := o.one()
		if err != nil {
			return nil, "", err
		}
		id, err := ids.Validate(arg)
		if err != nil {
			return nil, "", err
		}
		result, err := req(cl, "POST", "/v1/rm", nil, map[string]any{"project": c.Project, "id": id, "message": emptyToNil(o.flag("message"))}, headers(keys.Remove(id, o.flag("expect"), o.flag("message")), o.flag("expect")))
		return result, fmt.Sprintf("queued rm %s as %s", id, stringField(result, "receipt")), err
	case "mv":
		if err := o.count(2, 2); err != nil {
			return nil, "", err
		}
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
		key := ""
		if !o.has("dry-run") {
			key = keys.Move(src, dst, o.flag("expect-head"), o.flag("message"))
		}
		result, err := req(cl, "POST", "/v1/mv", nil, map[string]any{"project": c.Project, "source": src, "destination": dst, "message": emptyToNil(o.flag("message")), "dry_run": o.has("dry-run")}, headers(key, o.flag("expect-head")))
		human := fmt.Sprintf("queued mv %s to %s as %s", src, dst, stringField(result, "receipt"))
		if o.has("dry-run") {
			human = fmt.Sprintf("would move %s to %s", src, dst)
		}
		return result, human, err
	case "pointer":
		if o.sub != "add" || len(o.positional) != 2 {
			return nil, "", memerr.New("usage_error", "use: mem pointer add EXPRESSION ID", nil)
		}
		p, err := pointers.Parse(o.positional[0], false, "")
		if err != nil {
			return nil, "", err
		}
		id, err := ids.Validate(o.positional[1])
		if err != nil {
			return nil, "", err
		}
		return uploadOne(cl, c, id, "pointer.md", []byte(p.Canonical()+"\n"), "pointer", "", o.flag("expect"), o.flag("message"), "")
	case "add":
		return addFiles(o, c, cl)
	}
	return nil, "", memerr.New("usage_error", "unknown write command", nil)
}
func emptyToNil(value string) any {
	if value == "" {
		return nil
	}
	return value
}
func uploadOne(cl *transport.Client, c config.Config, id, filename string, content []byte, kind, origin, expect, message, warning string) (any, string, error) {
	fields := map[string]string{"project": c.Project, "path": id, "kind": kind}
	if origin != "" {
		fields["origin_path"] = origin
	}
	if message != "" {
		fields["message"] = message
	}
	result, err := cl.Multipart("/v1/add", fields, filename, content, headers(keys.Add(digest(content), id, expect, origin, message), expect))
	if err != nil {
		return nil, "", err
	}
	r := object(result)
	if warning != "" {
		r["warning"] = warning
	}
	verb := "queued"
	if r["idempotent_replay"] == true {
		verb = "replayed"
	}
	human := fmt.Sprintf("%s %s as %s", verb, id, str(r["receipt"]))
	if warning != "" {
		human += "; " + warning
	}
	return r, human, nil
}

type item struct {
	ID, Filename, Kind, Origin, SHA string
	Content                         []byte
	Path                            string
}

func addFiles(o options, c config.Config, cl *transport.Client) (any, string, error) {
	if err := o.count(1, -1); err != nil {
		return nil, "", err
	}
	if o.has("pointer") && len(o.positional) == 2 && strings.HasPrefix(o.positional[0], "@") {
		fmt.Fprintln(os.Stderr, "mem: deprecated: use `mem pointer add EXPRESSION ID`")
		if o.flag("from-git") != "" {
			return nil, "", memerr.New("usage_error", "--from-git reads committed file content and so cannot be combined with --pointer", nil)
		}
		p, err := pointers.Parse(o.positional[0], false, "")
		if err != nil {
			return nil, "", err
		}
		rawID := o.positional[1]
		if o.flag("as") != "" {
			rawID = o.flag("as")
		}
		id, err := ids.Validate(rawID)
		if err != nil {
			return nil, "", err
		}
		return uploadOne(cl, c, id, "pointer.md", []byte(p.Canonical()+"\n"), "pointer", "", o.flag("expect"), o.flag("message"), "")
	}
	if len(o.positional) > 1 && (o.flag("as") != "" || o.flag("expect") != "" || o.flag("from-git") != "") {
		return nil, "", memerr.New("usage_error", "--as, --expect, and --from-git require one file", nil)
	}
	if len(o.positional) > 1 && o.flag("message") == "" {
		return nil, "", memerr.New("usage_error", "multiple files require -m/--message", nil)
	}
	if o.has("pointer") && o.flag("from-git") != "" {
		return nil, "", memerr.New("usage_error", "--pointer cannot be combined with --from-git", nil)
	}
	base := filepath.Dir(c.MarkerPath)
	policyRaw, err := req(cl, "GET", "/v1/policy", nil, nil, nil)
	if err != nil {
		return nil, "", err
	}
	policy := object(policyRaw)
	allowed := map[string]bool{}
	if extensions, ok := policy["inline_extensions"].([]any); ok {
		for _, v := range extensions {
			allowed[str(v)] = true
		}
	}
	limit := int64(0)
	if value, ok := policy["max_upload_bytes"].(float64); ok {
		limit = int64(value)
	}
	seen := map[string]bool{}
	items := []item{}
	for _, raw := range o.positional {
		info, err := os.Stat(raw)
		if err != nil || !info.Mode().IsRegular() {
			return nil, "", memerr.New("invalid_request", "not a file: "+raw, nil)
		}
		abs, err := filepath.Abs(raw)
		if err != nil {
			return nil, "", err
		}
		if devicehome.IsDeviceHome(abs) {
			return nil, "", memerr.New("invalid_request", "device-home files are private and cannot be added to project memory", nil)
		}
		id, err := derive(abs, base, o.flag("as"))
		if err != nil {
			return nil, "", err
		}
		if seen[id] {
			return nil, "", memerr.New("invalid_request", "duplicate id in batch: "+id, nil)
		}
		seen[id] = true
		ext := strings.ToLower(filepath.Ext(abs))
		inline := !o.has("pointer") && allowed[ext]
		it := item{ID: id, Filename: filepath.Base(abs), Origin: abs}
		if inline {
			if limit > 0 && info.Size() > limit {
				return nil, "", memerr.New("invalid_request", abs+" exceeds the inline upload limit; use --pointer", nil)
			}
			if ext != ".md" && !strings.HasSuffix(id, ext) {
				return nil, "", memerr.New("invalid_request", "text id must retain the "+ext+" extension", nil)
			}
			it.Kind = "text"
			if ext == ".md" {
				it.Kind = "note"
			}
			if rev := o.flag("from-git"); rev != "" {
				// The commit is the source of truth; an invalid working copy
				// must not block a valid committed revision. The size gate
				// applies to the bytes actually uploaded.
				it.Content, err = committedBytes(abs, rev)
				if err != nil {
					return nil, "", err
				}
				if limit > 0 && int64(len(it.Content)) > limit {
					return nil, "", memerr.New("invalid_request", fmt.Sprintf("%s is %d bytes at %s, over the inline upload limit; use --pointer", id, len(it.Content), rev), nil)
				}
			} else {
				it.Content, err = os.ReadFile(abs)
				if err != nil {
					return nil, "", memerr.New("internal_error", err.Error(), nil)
				}
			}
			if it.Kind == "note" {
				if _, err := summary.Validate(string(it.Content)); err != nil {
					return nil, "", err
				}
			}
		} else {
			if c.Credentials == nil || c.Credentials.Alias == "" {
				return nil, "", memerr.New("unauthorized", "this enrollment needs a device alias to create a pointer", nil)
			}
			p, err := pointers.Parse("@file("+c.Credentials.Alias+":"+abs+")", false, "")
			if err != nil {
				return nil, "", err
			}
			it.Content = []byte(p.Canonical() + "\n")
			it.Kind = "pointer"
			it.Origin = ""
		}
		it.SHA = digest(it.Content)
		if inline && len(o.positional) > 1 {
			it.Path = abs
		}
		items = append(items, it)
	}
	if len(items) == 1 {
		it := items[0]
		warning := ""
		if it.Kind == "pointer" {
			warning = fmt.Sprintf("%s content was not stored; only its pointer is tracked", it.Filename)
		}
		return uploadOne(cl, c, it.ID, it.Filename, it.Content, it.Kind, it.Origin, o.flag("expect"), o.flag("message"), warning)
	}
	metadata := []map[string]any{}
	uploads := []transport.Upload{}
	hash := sha256.New()
	hash.Write([]byte(c.Project))
	hash.Write([]byte(o.flag("message")))
	for _, it := range items {
		metadata = append(metadata, map[string]any{"id": it.ID, "filename": it.Filename, "kind": it.Kind, "origin_path": emptyToNil(it.Origin), "sha256": it.SHA})
		u := transport.Upload{Filename: it.Filename, Content: it.Content, Path: it.Path}
		uploads = append(uploads, u)
		for _, v := range []string{it.ID, it.Kind, it.Filename, it.Origin, it.SHA} {
			hash.Write([]byte(v))
			hash.Write([]byte{0})
		}
	}
	encoded, _ := json.Marshal(metadata)
	result, err := cl.MultipartMany(map[string]string{"project": c.Project, "message": o.flag("message"), "items": string(encoded)}, uploads, headers(hex.EncodeToString(hash.Sum(nil))+":add-many", ""))
	return result, fmt.Sprintf("queued %d files as %s", len(items), stringField(result, "receipt")), err
}
func committedBytes(path, rev string) ([]byte, error) {
	dir := filepath.Dir(path)
	cmd := exec.Command("git", "rev-parse", "--show-toplevel")
	cmd.Dir = dir
	rootBytes, err := cmd.Output()
	if err != nil {
		return nil, memerr.New("invalid_request", "--from-git needs a git repository containing "+path, nil)
	}
	root := strings.TrimSpace(string(rootBytes))
	rel, err := filepath.Rel(root, path)
	if err != nil || strings.HasPrefix(rel, "..") {
		return nil, memerr.New("invalid_request", path+" is not inside the repository at "+root, nil)
	}
	cmd = exec.Command("git", "show", rev+":"+filepath.ToSlash(rel))
	cmd.Dir = root
	data, err := cmd.Output()
	if err != nil {
		return nil, memerr.New("invalid_request", "cannot read committed file at "+rev, nil)
	}
	return data, nil
}
