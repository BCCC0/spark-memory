package memerr

import "testing"

func TestEveryCodeHasHTTPAndExit(t *testing.T) {
	wantExit := map[string]int{
		"unauthorized": 8, "device_exists": 8, "server_unreachable": 2,
		"lock_held": 3, "lock_timeout": 3, "target_busy": 3, "queue_full": 3,
		"queued_write_failed": 1, "schema_invalid": 4, "staging_diverged": 5,
		"no_project": 6, "stale_revision": 7, "target_moved": 9, "not_found": 9,
		"invalid_request": 4, "origin_outside_root": 4,
		"origin_not_found": 9, "ambiguous_origin": 4, "local_file_missing": 4,
		"local_path_exists": 4, "cross_project_move": 4,
		"internal_error": 1, "usage_error": 64,
	}
	if len(Table) != len(wantExit) {
		t.Fatalf("table has %d codes, want %d", len(Table), len(wantExit))
	}
	for code, exit := range wantExit {
		definition, ok := Table[code]
		if !ok {
			t.Errorf("missing %q", code)
			continue
		}
		if definition.Exit != exit {
			t.Errorf("%s exit = %d, want %d", code, definition.Exit, exit)
		}
		if definition.HTTP == 0 {
			t.Errorf("%s has no HTTP status", code)
		}
	}
}

func TestUnknownCodeBecomesInternalError(t *testing.T) {
	err := New("not_a_code", "boom", nil)
	if err.Code != "internal_error" || err.ExitCode() != 1 {
		t.Fatalf("New unknown = %#v", err)
	}
}
