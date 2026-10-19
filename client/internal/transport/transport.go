package transport

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"mime/multipart"
	"net/http"
	"net/url"
	"os"
	"strings"
	"time"

	"github.com/BCCC0/spark-memory/client/internal/memerr"
)

type Client struct {
	Base, Token string
	HTTP        *http.Client
}

func New(base, token string, timeout time.Duration) (*Client, error) {
	u, err := url.Parse(base)
	if err != nil || (u.Scheme != "http" && u.Scheme != "https") || u.Host == "" {
		return nil, memerr.New("invalid_request", "invalid server URL: "+base, nil)
	}
	return &Client{Base: strings.TrimRight(base, "/"), Token: token, HTTP: &http.Client{Timeout: timeout}}, nil
}
func (c *Client) Request(method, path string, query url.Values, body io.Reader, contentType string, headers http.Header, auth bool) (any, error) {
	return c.request(c.HTTP, method, path, query, body, contentType, headers, auth)
}

func (c *Client) request(httpClient *http.Client, method, path string, query url.Values, body io.Reader, contentType string, headers http.Header, auth bool) (any, error) {
	target := c.Base + path
	if len(query) != 0 {
		target += "?" + query.Encode()
	}
	req, err := http.NewRequestWithContext(context.Background(), method, target, body)
	if err != nil {
		return nil, memerr.New("invalid_request", err.Error(), nil)
	}
	req.Header.Set("Accept", "application/json")
	req.Header.Set("User-Agent", "mem/0.1")
	if contentType != "" {
		req.Header.Set("Content-Type", contentType)
	}
	if auth && c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}
	for name, values := range headers {
		for _, value := range values {
			req.Header.Add(name, value)
		}
	}
	res, err := httpClient.Do(req)
	if err != nil {
		return nil, memerr.New("server_unreachable", "cannot reach "+c.Base+": "+err.Error(), map[string]any{"server": c.Base})
	}
	defer res.Body.Close()
	data, err := io.ReadAll(res.Body)
	if err != nil {
		return nil, memerr.New("internal_error", err.Error(), nil)
	}
	if res.StatusCode >= 400 {
		return nil, responseError(res.StatusCode, data)
	}
	if len(data) == 0 {
		return nil, nil
	}
	var result any
	if err := json.Unmarshal(data, &result); err != nil {
		return nil, memerr.New("internal_error", fmt.Sprintf("server returned non-JSON (%d)", res.StatusCode), nil)
	}
	return result, nil
}
func responseError(status int, data []byte) error {
	var envelope struct {
		Error memerr.Error `json:"error"`
	}
	if json.Unmarshal(data, &envelope) == nil && envelope.Error.Code != "" {
		return memerr.New(envelope.Error.Code, envelope.Error.Message, envelope.Error.Detail)
	}
	codes := map[int]string{401: "unauthorized", 404: "no_project", 412: "stale_revision", 422: "schema_invalid", 423: "lock_held", 503: "server_unreachable"}
	code := codes[status]
	if code == "" {
		code = "internal_error"
	}
	detail := map[string]any{}
	if len(data) > 0 {
		if len(data) > 400 {
			data = data[:400]
		}
		detail["body"] = string(data)
	}
	return memerr.New(code, fmt.Sprintf("HTTP %d from server", status), detail)
}
func (c *Client) JSON(method, path string, query url.Values, payload any, headers http.Header) (any, error) {
	var body io.Reader
	contentType := ""
	if payload != nil {
		data, err := json.Marshal(payload)
		if err != nil {
			return nil, err
		}
		body = bytes.NewReader(data)
		contentType = "application/json"
	}
	return c.Request(method, path, query, body, contentType, headers, true)
}

// Raw fetches bytes from a non-JSON endpoint such as /raw/{project}/{id}.
func (c *Client) Raw(path string) ([]byte, error) {
	req, err := http.NewRequestWithContext(context.Background(), "GET", c.Base+path, nil)
	if err != nil {
		return nil, memerr.New("invalid_request", err.Error(), nil)
	}
	req.Header.Set("Accept", "text/markdown")
	req.Header.Set("User-Agent", "mem/0.1")
	if c.Token != "" {
		req.Header.Set("Authorization", "Bearer "+c.Token)
	}
	res, err := c.HTTP.Do(req)
	if err != nil {
		return nil, memerr.New("server_unreachable", "cannot reach "+c.Base+": "+err.Error(), map[string]any{"server": c.Base})
	}
	defer res.Body.Close()
	data, err := io.ReadAll(res.Body)
	if err != nil {
		return nil, memerr.New("internal_error", err.Error(), nil)
	}
	if res.StatusCode >= 400 {
		return nil, responseError(res.StatusCode, data)
	}
	return data, nil
}
func (c *Client) Multipart(path string, fields map[string]string, filename string, data []byte, headers http.Header) (any, error) {
	var buffer bytes.Buffer
	w := multipart.NewWriter(&buffer)
	for key, value := range fields {
		if err := w.WriteField(key, value); err != nil {
			return nil, err
		}
	}
	part, err := w.CreateFormFile("file", filename)
	if err != nil {
		return nil, err
	}
	if _, err := part.Write(data); err != nil {
		return nil, err
	}
	if err := w.Close(); err != nil {
		return nil, err
	}
	return c.Request("POST", path, nil, &buffer, w.FormDataContentType(), headers, true)
}

type Upload struct {
	Filename string
	Path     string
	Content  []byte
}

func (c *Client) MultipartMany(fields map[string]string, uploads []Upload, headers http.Header) (any, error) {
	reader, writer := io.Pipe()
	defer reader.Close()
	multi := multipart.NewWriter(writer)
	go func() {
		var err error
		defer func() {
			if err == nil {
				err = multi.Close()
			}
			writer.CloseWithError(err)
		}()
		for key, value := range fields {
			if err = multi.WriteField(key, value); err != nil {
				return
			}
		}
		for i, item := range uploads {
			var part io.Writer
			part, err = multi.CreateFormFile(fmt.Sprintf("file%d", i), item.Filename)
			if err != nil {
				return
			}
			if item.Path != "" {
				var file io.ReadCloser
				file, err = os.Open(item.Path)
				if err != nil {
					return
				}
				_, err = io.Copy(part, file)
				file.Close()
				if err != nil {
					return
				}
			} else {
				_, err = part.Write(item.Content)
				if err != nil {
					return
				}
			}
		}
	}()
	// A streamed batch may legitimately outlive the interactive timeout: the
	// Python client's timeout applies per socket operation, not to the whole
	// upload. Give the batch a generous overall allowance.
	client := c.HTTP
	if client.Timeout > 0 && client.Timeout < 10*time.Minute {
		clone := *client
		clone.Timeout = 10 * time.Minute
		client = &clone
	}
	return c.request(client, "POST", "/v1/add-many", nil, reader, multi.FormDataContentType(), headers, true)
}
