package memerr

import "fmt"

type Definition struct{ HTTP, Exit int }

var Table = map[string]Definition{
	"unauthorized": {401, 8}, "device_exists": {409, 8},
	"server_unreachable": {503, 2}, "lock_held": {423, 3},
	"lock_timeout": {423, 3}, "target_busy": {409, 3},
	"queue_full": {503, 3}, "queued_write_failed": {409, 1},
	"schema_invalid": {422, 4}, "staging_diverged": {409, 5},
	"no_project": {404, 6}, "stale_revision": {412, 7},
	"target_moved": {410, 9}, "not_found": {404, 9},
	"invalid_request": {400, 4}, "origin_outside_root": {400, 4}, "internal_error": {500, 1},
	"usage_error": {400, 64},
}

type Error struct {
	Code       string         `json:"code"`
	Message    string         `json:"message"`
	Detail     map[string]any `json:"detail"`
	HTTPStatus int            `json:"-"`
}

func New(code, message string, detail map[string]any) *Error {
	if _, ok := Table[code]; !ok {
		code = "internal_error"
	}
	if detail == nil {
		detail = map[string]any{}
	}
	return &Error{Code: code, Message: message, Detail: detail, HTTPStatus: Table[code].HTTP}
}

func (e *Error) Error() string { return fmt.Sprintf("%s: %s", e.Code, e.Message) }
func (e *Error) ExitCode() int { return Table[e.Code].Exit }
