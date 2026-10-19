package cli

import (
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/BCCC0/spark-memory/client/internal/config"
	"github.com/BCCC0/spark-memory/client/internal/memerr"
	"github.com/BCCC0/spark-memory/client/internal/transport"
)

func TestSplitIDMatchesPythonSelectors(t *testing.T) {
	for _, test := range []struct {
		raw, explicit string
		want          []string
	}{
		{"notes/topic@rev:01- 2", "", []string{"notes/topic", "rev", "", "1-2"}},
		{"topic@last#heading@literal", "", []string{"topic", "last", "heading@literal", ""}},
		{"topic@last", "override", []string{"topic", "override", "", ""}},
		{"topic:1--2", "", []string{"topic", "", "", "1--2"}},
	} {
		id, rev, section, span, err := splitID(test.raw, test.explicit)
		if err != nil {
			t.Fatalf("%q: %v", test.raw, err)
		}
		if got := []string{id, rev, section, span}; !reflect.DeepEqual(got, test.want) {
			t.Errorf("%q: got %q, want %q", test.raw, got, test.want)
		}
	}
}

func TestSplitIDRejectsMalformedRangeBeforeRequest(t *testing.T) {
	for _, raw := range []string{"topic:1-x", "topic:1-2-3", "topic:1", "topic:-"} {
		_, _, _, _, err := splitID(raw, "")
		e, ok := err.(*memerr.Error)
		if !ok || e.Code != "usage_error" {
			t.Errorf("%q: expected usage_error, got %v", raw, err)
		}
	}
}

func TestReadOneRejectsEmptyOutlineSection(t *testing.T) {
	cl, _ := transport.New("http://127.0.0.1:1", "", 0)
	_, _, err := readOne(options{flags: map[string]string{"outline": "true"}}, config.Config{}, cl, "topic#")
	e, ok := err.(*memerr.Error)
	if !ok || e.Code != "usage_error" {
		t.Fatalf("expected usage_error, got %v", err)
	}
}

func TestReadOneRejectsLinkOnlyWithAnySliceSelector(t *testing.T) {
	cl, _ := transport.New("http://127.0.0.1:1", "", 0)
	for _, raw := range []string{"topic#", "topic:1-2", "topic#Heading"} {
		_, _, err := readOne(options{flags: map[string]string{"link-only": "true"}}, config.Config{}, cl, raw)
		e, ok := err.(*memerr.Error)
		if !ok || e.Code != "usage_error" {
			t.Errorf("%q: expected usage_error, got %v", raw, err)
		}
	}
}

func TestReadOnePendingHumanIncludesCommittedRevision(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"status":"pending","receipt":"r1","committed_revision":"abc"}`))
	}))
	defer server.Close()
	cl, err := transport.New(server.URL, "", 0)
	if err != nil {
		t.Fatal(err)
	}
	_, human, err := readOne(options{flags: map[string]string{}}, config.Config{Project: "p", MaxBytes: 1024}, cl, "topic")
	if err != nil {
		t.Fatal(err)
	}
	if human != "topic: pending as r1 (committed: abc)" {
		t.Fatalf("unexpected human output: %q", human)
	}
}

func TestReadOneReportsFreshnessForAWholeNoteAndASlice(t *testing.T) {
	local := filepath.Join(t.TempDir(), "note.md")
	text := "---\nsummary: s\n---\n\n# T\n\nhello\n"
	if err := os.WriteFile(local, []byte(text), 0644); err != nil {
		t.Fatal(err)
	}
	body := "# T\n\nhello"
	sum := sha256.Sum256([]byte(body))
	digest := hex.EncodeToString(sum[:])

	for _, selector := range []string{"fresh", "fresh:1-2"} {
		server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			w.Header().Set("Content-Type", "application/json")
			_, _ = w.Write([]byte(`{"id":"fresh","kind":"note","sha256":"commit","body_sha256":"` + digest + `","origin_path":"` + filepath.ToSlash(local) + `","note_updated":"2026-01-01T00:00:00Z","body":"hello"}`))
		}))
		cl, err := transport.New(server.URL, "", 0)
		if err != nil {
			server.Close()
			t.Fatal(err)
		}
		result, _, err := readOne(options{flags: map[string]string{}}, config.Config{Project: "p", MaxBytes: 1024}, cl, selector)
		server.Close()
		if err != nil {
			t.Fatalf("%s: %v", selector, err)
		}
		record := object(result)
		if record["body_matches"] != true {
			t.Errorf("%s: body_matches = %v, record=%v", selector, record["body_matches"], record)
		}
		if record["local_file"] != true {
			t.Errorf("%s: local_file = %v", selector, record["local_file"])
		}
		if _, ok := record["body_sha256"]; ok {
			t.Errorf("%s: body_sha256 leaked into CLI output", selector)
		}
		if _, ok := record["local_sha256"]; ok {
			t.Errorf("%s: local_sha256 leaked into CLI output", selector)
		}
		if record["sha256"] != "commit" {
			t.Errorf("%s: sha256 = %v", selector, record["sha256"])
		}
	}
}
