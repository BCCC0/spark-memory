package cli

import (
	"net/http"
	"net/http/httptest"
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
