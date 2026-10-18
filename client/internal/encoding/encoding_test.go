package encoding

import (
	"strings"
	"testing"
)

func TestRuneCountMatchesPythonLen(t *testing.T) {
	cases := map[string]int{
		"":                       0,
		"hello":                  5,
		"界":                      1,
		"😀":                      1,
		"a\u0301":                2,
		strings.Repeat("界", 200): 200,
	}
	for value, want := range cases {
		if got := RuneCount(value); got != want {
			t.Errorf("RuneCount(%q) = %d, want %d", value, got, want)
		}
	}
}
