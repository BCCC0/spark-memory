package conformance

import (
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"reflect"
	"testing"

	"github.com/BCCC0/spark-memory/client/internal/config"
	"github.com/BCCC0/spark-memory/client/internal/ids"
	"github.com/BCCC0/spark-memory/client/internal/keys"
	"github.com/BCCC0/spark-memory/client/internal/memerr"
	"github.com/BCCC0/spark-memory/client/internal/paths"
	"github.com/BCCC0/spark-memory/client/internal/pointers"
	"github.com/BCCC0/spark-memory/client/internal/summary"
)

type vector struct {
	Kind     string          `json:"kind"`
	Role     string          `json:"role"`
	Input    json.RawMessage `json:"input"`
	Expected json.RawMessage `json:"expected"`
}
type file struct {
	SourceRevision string           `json:"source_revision"`
	ErrorTable     map[string][]int `json:"error_table"`
	Cases          []vector         `json:"cases"`
}

func result(value string, err error) map[string]any {
	if err == nil {
		return map[string]any{"ok": true, "value": value}
	}
	var typed *memerr.Error
	if errors.As(err, &typed) {
		return map[string]any{"ok": false, "code": typed.Code, "message": typed.Message, "detail": typed.Detail}
	}
	return map[string]any{"ok": false, "code": "internal_error"}
}

func TestPythonContractVectors(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("..", "..", "testdata", "vectors", "rules.json"))
	if err != nil {
		t.Fatal(err)
	}
	var corpus file
	if err := json.Unmarshal(data, &corpus); err != nil {
		t.Fatal(err)
	}
	if corpus.SourceRevision == "" {
		t.Error("vector manifest has no source_revision")
	}
	for code, pair := range corpus.ErrorTable {
		definition, ok := memerr.Table[code]
		if !ok || len(pair) != 2 || definition.HTTP != pair[0] || definition.Exit != pair[1] {
			t.Errorf("error table %s: Go=%v Python=%v", code, definition, pair)
		}
	}
	for i, test := range corpus.Cases {
		if test.Role != "contract" && test.Role != "port-oracle" {
			t.Errorf("case %d has unknown role %q", i, test.Role)
		}
		var got any
		switch test.Kind {
		case "id", "project", "pointer", "summary":
			var input string
			if err := json.Unmarshal(test.Input, &input); err != nil {
				t.Fatal(err)
			}
			switch test.Kind {
			case "id":
				value, e := ids.Validate(input)
				got = result(value, e)
			case "project":
				value, e := ids.Project(input)
				got = result(value, e)
			case "pointer":
				p, e := pointers.Parse(input, false, "")
				got = result(p.Canonical(), e)
			case "summary":
				value, e := summary.Validate(input)
				got = result(value, e)
			}
		case "from_path":
			var input string
			if err := json.Unmarshal(test.Input, &input); err != nil {
				t.Fatal(err)
			}
			got = ids.FromPath(input)
		case "add_key", "rm_key", "mv_key":
			var input []string
			if err := json.Unmarshal(test.Input, &input); err != nil {
				t.Fatal(err)
			}
			switch test.Kind {
			case "add_key":
				got = keys.Add(input[0], input[1], input[2], input[3], input[4])
			case "rm_key":
				got = keys.Remove(input[0], input[1], input[2])
			case "mv_key":
				got = keys.Move(input[0], input[1], input[2], input[3])
			}
		case "path":
			var input string
			if err := json.Unmarshal(test.Input, &input); err != nil {
				t.Fatal(err)
			}
			var windows, wsl any
			if value := paths.ToWindows(input); value != "" {
				windows = value
			}
			if value := paths.ToWSL(input); value != "" {
				wsl = value
			}
			got = map[string]any{"windows": windows, "wsl": wsl}
		case "marker":
			var input string
			if err := json.Unmarshal(test.Input, &input); err != nil {
				t.Fatal(err)
			}
			marker, e := config.ParseMarker([]byte(input))
			if e != nil {
				got = map[string]any{"ok": false, "code": "invalid_request"}
			} else {
				patterns := []any{}
				for _, pattern := range marker.Track.Patterns {
					patterns = append(patterns, pattern)
				}
				got = map[string]any{"ok": true, "value": map[string]any{
					"project": marker.Project, "server": marker.Server, "patterns": patterns,
				}}
			}
		default:
			t.Fatalf("unknown vector kind %q", test.Kind)
		}
		var want any
		if err := json.Unmarshal(test.Expected, &want); err != nil {
			t.Fatal(err)
		}
		if !reflect.DeepEqual(got, want) {
			t.Errorf("case %d %s input=%s: got %#v, want %#v", i, test.Kind, test.Input, got, want)
		}
	}
}
