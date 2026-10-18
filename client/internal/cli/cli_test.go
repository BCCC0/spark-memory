package cli

import (
	"testing"

	"github.com/BCCC0/spark-memory/client/internal/memerr"
)

func TestParseCommonFlagsAroundVerb(t *testing.T) {
	for _, args := range [][]string{{"--server", "http://host", "--human", "read", "note"}, {"read", "note", "--human", "--server", "http://host"}} {
		o, err := parse(args)
		if err != nil {
			t.Fatal(err)
		}
		if o.command != "read" || len(o.positional) != 1 || o.positional[0] != "note" || !o.human || o.server != "http://host" {
			t.Fatalf("parsed %#v as %#v", args, o)
		}
	}
}
func TestParserMatchesArgparseGrammar(t *testing.T) {
	for _, args := range [][]string{
		{"bogus", "--help"},
		{"hook", "bogus", "--help"},
		{"--human=false", "read", "x"},
		{"--status", "x", "ls"},
		{"-mfoo", "status"},
	} {
		if _, err := parse(args); err == nil {
			t.Errorf("%#v was accepted", args)
		}
	}
	o, err := parse([]string{"read", "--", "-weird"})
	if err != nil {
		t.Fatal(err)
	}
	if len(o.positional) != 1 || o.positional[0] != "-weird" {
		t.Fatalf("-- did not stop option parsing: %#v", o.positional)
	}
	o, err = parse([]string{"add", "-mmessage", "f.md"})
	if err != nil {
		t.Fatal(err)
	}
	if o.flag("message") != "message" {
		t.Fatalf("attached -m parsed as %q", o.flag("message"))
	}
	o, err = parse([]string{"--hum", "status"})
	if err != nil {
		t.Fatal(err)
	}
	if !o.human {
		t.Fatal("abbreviated --human was not applied")
	}
}

func TestParserRejectsUsageBeforeConfiguration(t *testing.T) {
	for _, args := range [][]string{{"read"}, {"ls", "--bogus"}, {"search", "--mode", "unknown", "query"}, {"status", "extra"}, {"rm", "id", "--limit", "2"}} {
		_, err := parse(args)
		if e, ok := err.(*memerr.Error); !ok || e.Code != "usage_error" || e.ExitCode() != 64 {
			t.Errorf("%#v: %v", args, err)
		}
	}
}
