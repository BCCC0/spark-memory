package transport

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/BCCC0/spark-memory/client/internal/memerr"
)

func TestMultipartRequestAndTypedResponse(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != "POST" || r.URL.Path != "/v1/add" {
			t.Errorf("request target: %s %s", r.Method, r.URL.Path)
		}
		if got := r.Header.Get("Authorization"); got != "Bearer token" {
			t.Errorf("authorization: %q", got)
		}
		if got := r.Header.Get("Idempotency-Key"); got != "key" {
			t.Errorf("idempotency key: %q", got)
		}
		if got := r.Header.Get("If-Match"); got != "rev" {
			t.Errorf("precondition: %q", got)
		}
		if got := r.Header.Get("User-Agent"); got != "mem/0.2" {
			t.Errorf("user agent: %q", got)
		}
		if err := r.ParseMultipartForm(1024); err != nil {
			t.Error(err)
			return
		}
		if got := r.FormValue("path"); got != "ops/boot" {
			t.Errorf("path: %q", got)
		}
		file, header, err := r.FormFile("file")
		if err != nil {
			t.Error(err)
			return
		}
		defer file.Close()
		if header.Filename != "boot.md" {
			t.Errorf("filename: %q", header.Filename)
		}
		if media := header.Header.Get("Content-Type"); media != "application/octet-stream" {
			t.Errorf("file content type: %q", media)
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(202)
		json.NewEncoder(w).Encode(map[string]any{"status": "queued", "receipt": "r1"})
	}))
	defer server.Close()
	client, err := New(server.URL, "token", time.Second)
	if err != nil {
		t.Fatal(err)
	}
	header := http.Header{}
	header.Set("Idempotency-Key", "key")
	header.Set("If-Match", "rev")
	response, err := client.Multipart("/v1/add", map[string]string{"path": "ops/boot"}, "boot.md", []byte("text"), header)
	if err != nil {
		t.Fatal(err)
	}
	if response.(map[string]any)["receipt"] != "r1" {
		t.Fatalf("response: %#v", response)
	}
}
func TestBadURLAndErrorCode(t *testing.T) {
	if _, err := New("127.0.0.1:8321", "", time.Second); err == nil {
		t.Fatal("accepted URL without scheme")
	} else if e, ok := err.(*memerr.Error); !ok || e.Code != "invalid_request" {
		t.Fatalf("wrong error: %v", err)
	}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(409)
		w.Write([]byte(`{"error":{"code":"target_busy","message":"busy","detail":{"id":"n"}}}`))
	}))
	defer server.Close()
	client, _ := New(server.URL, "", time.Second)
	_, err := client.JSON("GET", "/v1/notes", nil, nil, nil)
	if e, ok := err.(*memerr.Error); !ok || e.Code != "target_busy" || e.ExitCode() != 3 {
		t.Fatalf("wrong typed error: %v", err)
	}
}
func TestStreamingMany(t *testing.T) {
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasPrefix(r.Header.Get("Content-Type"), "multipart/form-data;") {
			t.Errorf("content type: %s", r.Header.Get("Content-Type"))
		}
		reader, err := r.MultipartReader()
		if err != nil {
			t.Error(err)
			return
		}
		parts := 0
		for {
			part, err := reader.NextPart()
			if err != nil {
				break
			}
			if part.FormName() == "file0" || part.FormName() == "file1" {
				parts++
			}
			part.Close()
		}
		if parts != 2 {
			t.Errorf("file parts: %d", parts)
		}
		w.Write([]byte(`{"receipt":"r2"}`))
	}))
	defer server.Close()
	client, _ := New(server.URL, "", time.Second)
	_, err := client.MultipartMany(map[string]string{"project": "p"}, []Upload{{Filename: "a.md", Content: []byte("a")}, {Filename: "b.md", Content: []byte("b")}}, nil)
	if err != nil {
		t.Fatal(err)
	}
}
