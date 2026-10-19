package cli

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	"github.com/BCCC0/spark-memory/client/internal/memerr"
	"github.com/BCCC0/spark-memory/client/internal/testenv"
	"github.com/BCCC0/spark-memory/client/internal/transport"
)

func readEnv(t *testing.T) *testenv.Env {
	env := testenv.New(t)
	env.Enroll("dev")
	return env
}

func TestReadCommandsAgainstFakeDaemon(t *testing.T) {
	env := readEnv(t)
	d := env.Daemon
	d.Handle("GET", "/v1/status", 200, map[string]any{
		"project": "demo", "head": "abc", "message": "m", "lock": map[string]any{"held": false},
		"index_dirty": false, "read": map[string]any{"max_bytes": 4096},
	})
	d.Handle("GET", "/v1/notes", 200, map[string]any{"notes": []any{
		map[string]any{"id": "a", "status": "active", "type": "reference", "summary": "s"},
	}})
	d.Handle("GET", "/v1/notes/ops/boot", 200, map[string]any{
		"id": "ops/boot", "status": "committed", "kind": "note", "body": "hello", "bytes": 5, "truncated": false,
	})
	d.Handle("GET", "/v1/outline", 200, map[string]any{"headings": []any{
		map[string]any{"level": 1, "text": "Title"}, map[string]any{"level": 2, "text": "Sub"},
	}})
	d.Handle("GET", "/v1/manifest", 200, map[string]any{"revision": "abc", "entries": []any{
		map[string]any{"id": "a"},
	}})
	d.Handle("GET", "/v1/search", 200, map[string]any{"mode": "text", "hits": []any{
		map[string]any{"id": "a", "summary": "s", "line_range": []any{1, 2}, "matched_by": []any{"text"}},
	}})
	d.Handle("GET", "/v1/projects", 200, []any{
		map[string]any{"project": "demo", "notes": 1, "updated": "now"},
	})
	d.Handle("GET", "/v1/index/status", 200, map[string]any{
		"kind": "text", "head": "abc", "text": map[string]any{"indexed_head": "abc", "age": "0s"},
		"hybrid": map[string]any{"available": false},
	})
	d.Handle("GET", "/v1/devices", 200, []any{
		map[string]any{"device_id": "d1", "display_name": "test-device", "alias": "dev"},
	})
	d.Handle("GET", "/v1/queue", 200, map[string]any{"through": 1, "pending": 0, "indexed_head": "abc", "head": "abc", "failed": []any{}})

	status := env.MustMem("status")
	if status.JSON["project"] != "demo" {
		t.Errorf("status: %v", status.JSON)
	}
	ls := env.MustMem("ls")
	if notes, ok := ls.JSON["notes"].([]any); !ok || len(notes) != 1 {
		t.Errorf("ls: %v", ls.JSON)
	}
	read := env.MustMem("read", "ops/boot")
	if read.JSON["body"] != "hello" {
		t.Errorf("read: %v", read.JSON)
	}
	env.MustMem("read", "ops/boot", "--link-only")
	if got := d.LastRequest(t).Query.Get("link_only"); got != "true" {
		t.Errorf("link-only query = %q", got)
	}
	if got := d.Requests()[len(d.Requests())-1].RawPath; got != "/v1/notes/ops/boot" {
		t.Errorf("nested id was escaped: %q", got)
	}
	outline := env.MustMem("read", "ops/boot", "--outline")
	if !strings.Contains(outline.Stdout, "Title") {
		t.Errorf("outline: %s", outline.Stdout)
	}
	manifest := env.MustMem("manifest", "--human")
	if !strings.Contains(manifest.Stdout, "1 entries") {
		t.Errorf("manifest human: %s", manifest.Stdout)
	}
	search := env.MustMem("search", "hello")
	if hits, ok := search.JSON["hits"].([]any); !ok || len(hits) != 1 {
		t.Errorf("search: %v", search.JSON)
	}
	projects := env.MustMem("projects")
	if !json.Valid([]byte(projects.Stdout)) {
		t.Errorf("projects: %s", projects.Stdout)
	}
	index := env.MustMem("index")
	if index.JSON["kind"] != "text" {
		t.Errorf("index: %v", index.JSON)
	}
	devices := env.MustMem("devices")
	if !json.Valid([]byte(devices.Stdout)) {
		t.Errorf("devices: %s", devices.Stdout)
	}
	flush := env.MustMem("flush")
	if flush.JSON["status"] != "flushed" {
		t.Errorf("flush: %v", flush.JSON)
	}
}

func TestWriteCommandsAgainstFakeDaemon(t *testing.T) {
	env := readEnv(t)
	d := env.Daemon
	d.Handle("GET", "/v1/policy", 200, map[string]any{"inline_extensions": []any{".md", ".txt"}, "max_upload_bytes": 100000})
	d.Handle("POST", "/v1/add", 202, map[string]any{"status": "queued", "receipt": "r1"})
	d.Handle("POST", "/v1/add-many", 202, map[string]any{"status": "queued", "receipt": "r2"})
	d.Handle("POST", "/v1/rm", 202, map[string]any{"status": "queued", "receipt": "r3"})
	d.Handle("POST", "/v1/mv", 202, map[string]any{"status": "queued", "receipt": "r4"})
	env.Write("a.md", "---\nsummary: a\n---\nbody\n")

	added := env.MustMem("add", "a.md", "--as", "a", "-m", "message", "--expect", "rev")
	if added.JSON["receipt"] != "r1" {
		t.Errorf("add: %v", added.JSON)
	}
	last := d.LastRequest(t)
	if last.Headers.Get("Idempotency-Key") == "" || last.Headers.Get("If-Match") != "rev" {
		t.Errorf("add headers: %v", last.Headers)
	}
	if !strings.Contains(string(last.Body), `name="project"`) || !strings.Contains(string(last.Body), `filename="a.md"`) {
		t.Errorf("add body: %s", last.Body)
	}
	if !strings.Contains(string(last.Body), `name="origin_path"`) {
		t.Errorf("add without --no-origin should record an origin: %s", last.Body)
	}

	env.Write("no-origin.md", "---\nsummary: no-origin\n---\nbody\n")
	env.MustMem("add", "no-origin.md", "--as", "no-origin", "--no-origin")
	last = d.LastRequest(t)
	if strings.Contains(string(last.Body), `name="origin_path"`) {
		t.Errorf("--no-origin sent an origin: %s", last.Body)
	}

	env.Write("b.md", "---\nsummary: b\n---\nbody\n")
	many := env.MustMem("add", "a.md", "b.md", "-m", "batch")
	if many.JSON["receipt"] != "r2" {
		t.Errorf("add-many: %v", many.JSON)
	}
	last = d.LastRequest(t)
	if !strings.Contains(string(last.Body), `name="file0"`) || !strings.Contains(string(last.Body), `name="file1"`) {
		t.Errorf("add-many body: %s", last.Body)
	}

	rm := env.MustMem("rm", "a", "--expect", "sha", "-m", "remove")
	if rm.JSON["receipt"] != "r3" {
		t.Errorf("rm: %v", rm.JSON)
	}
	mv := env.MustMem("mv", "a", "b", "--dry-run")
	if mv.JSON["receipt"] != "r4" {
		t.Errorf("mv: %v", mv.JSON)
	}
	if got := d.LastRequest(t).Headers.Get("Idempotency-Key"); got != "" {
		t.Errorf("dry-run sent an idempotency key %q", got)
	}

	env.MustMem("pointer", "add", "@file(dev:/tmp/tool)", "tool")
	if got := d.LastRequest(t).Headers.Get("Idempotency-Key"); got == "" {
		t.Error("pointer add sent no idempotency key")
	}
	deprecated := env.MustMem("add", "--pointer", "@file(dev:/tmp/old)", "old")
	if !strings.Contains(deprecated.Stderr, "deprecated") {
		t.Errorf("deprecated add printed no warning: %s", deprecated.Stderr)
	}

	logout := env.MustMem("logout")
	if logout.JSON["removed"] != true {
		t.Errorf("logout: %v", logout.JSON)
	}
}

func TestLoginAndInitAgainstFakeDaemon(t *testing.T) {
	env := testenv.New(t)
	env.Daemon.Handle("POST", "/v1/enroll", 200, map[string]any{
		"device_id": "d2", "display_name": "test-device", "alias": "dev2", "token": "tok",
	})
	login := env.MustMem("login", "--server", env.Daemon.URL, "--name", "test-device", "--alias", "dev2", "--secret", "secret")
	if login.JSON["device_id"] != "d2" {
		t.Errorf("login: %v", login.JSON)
	}
	// init is idempotent when the marker already exists.
	init := env.MustMem("init")
	if init.JSON["created"] != false {
		t.Errorf("init: %v", init.JSON)
	}
}

func TestUsageErrorsAreJSONUnderHuman(t *testing.T) {
	env := testenv.New(t)
	for _, args := range [][]string{{"bogus", "--help"}, {"python", "--help"}, {"--human=false", "status"}} {
		result := env.Mem(args...)
		if result.Exit != 64 {
			t.Errorf("%v: exit %d, stderr=%s", args, result.Exit, result.Stderr)
		}
		if len(strings.TrimSpace(result.Stderr)) == 0 || !json.Valid([]byte(result.Stderr)) {
			t.Errorf("%v: stderr is not one JSON object: %s", args, result.Stderr)
		}
	}
}

func TestFlushTimesOutWithTypedError(t *testing.T) {
	env := testenv.New(t)
	env.Daemon.Handle("GET", "/v1/queue", 200, map[string]any{
		"through": 1, "pending": 1, "indexed_head": "old", "head": "new", "failed": []any{},
	})
	cl, err := transport.New(env.Daemon.URL, "", time.Second)
	if err != nil {
		t.Fatal(err)
	}
	previous := flushTimeout
	flushTimeout = 30 * time.Millisecond
	t.Cleanup(func() { flushTimeout = previous })
	_, _, err = flush(cl)
	typed, ok := err.(*memerr.Error)
	if !ok || typed.Code != "lock_timeout" || typed.ExitCode() != 3 {
		t.Fatalf("flush error = %v", err)
	}
}

func TestExitCodeMatrix(t *testing.T) {
	cases := map[string]int{
		"unauthorized": 8, "device_exists": 8, "server_unreachable": 2,
		"lock_held": 3, "lock_timeout": 3, "target_busy": 3, "queue_full": 3,
		"queued_write_failed": 1, "schema_invalid": 4, "staging_diverged": 5,
		"no_project": 6, "stale_revision": 7, "target_moved": 9, "not_found": 9,
		"invalid_request": 4, "origin_outside_root": 4, "internal_error": 1, "usage_error": 64,
	}
	for code, want := range cases {
		err := memerr.New(code, "test", nil)
		if got := err.ExitCode(); got != want {
			t.Errorf("%s: exit %d, want %d", code, got, want)
		}
	}
}
