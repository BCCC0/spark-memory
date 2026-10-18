// Package testenv provides the fake daemon and isolated CLI environment used
// by the Go black-box tests. It is an ordinary package so several packages can
// share it; it is only ever linked into test binaries.
package testenv

import (
	"io"
	"net/http"
	"net/http/httptest"
	"net/url"
	"sync"
	"testing"
)

type Request struct {
	Method  string
	Path    string
	RawPath string
	Query   url.Values
	Headers http.Header
	Body    []byte
}

type route struct {
	status  int
	body    any
	handler http.HandlerFunc
}

type Daemon struct {
	*httptest.Server
	mu       sync.Mutex
	requests []Request
	routes   map[string]route
}

func NewDaemon(t *testing.T) *Daemon {
	t.Helper()
	d := &Daemon{routes: map[string]route{}}
	d.Server = httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		body, _ := io.ReadAll(r.Body)
		d.mu.Lock()
		d.requests = append(d.requests, Request{
			Method: r.Method, Path: r.URL.Path, RawPath: r.URL.EscapedPath(), Query: r.URL.Query(),
			Headers: r.Header.Clone(), Body: body,
		})
		selected, ok := d.routes[r.Method+" "+r.URL.Path]
		d.mu.Unlock()
		if !ok {
			w.WriteHeader(http.StatusNotFound)
			_, _ = w.Write([]byte(`{"error":{"code":"not_found","message":"not scripted","detail":{}}}`))
			return
		}
		if selected.handler != nil {
			selected.handler(w, r)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		status := selected.status
		if status == 0 {
			status = http.StatusOK
		}
		w.WriteHeader(status)
		writeJSON(w, selected.body)
	}))
	t.Cleanup(d.Server.Close)
	return d
}

func (d *Daemon) Handle(method, path string, status int, body any) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.routes[method+" "+path] = route{status: status, body: body}
}

func (d *Daemon) HandleFunc(method, path string, handler http.HandlerFunc) {
	d.mu.Lock()
	defer d.mu.Unlock()
	d.routes[method+" "+path] = route{handler: handler}
}

func (d *Daemon) Requests() []Request {
	d.mu.Lock()
	defer d.mu.Unlock()
	out := make([]Request, len(d.requests))
	copy(out, d.requests)
	return out
}

func (d *Daemon) LastRequest(t *testing.T) Request {
	t.Helper()
	requests := d.Requests()
	if len(requests) == 0 {
		t.Fatal("no requests recorded")
	}
	return requests[len(requests)-1]
}
