package ids

import (
	"strings"
	"testing"
)

func TestValidateMatchesPython(t *testing.T) {
	valid := []string{"PROJECT", "alpha", "ops/boot", "x" + strings.Repeat("y", 79)}
	for _, value := range valid {
		if _, err := Validate(value); err != nil {
			t.Errorf("Validate(%q): %v", value, err)
		}
	}
	cases := []struct{ value, message string }{
		{"../x", "may not"},
		{".hidden", "may not start"},
		{"a//b", "empty directory"},
		{"A", "uppercase"},
		{"a bC", "uppercase"},
		{"Ä", "uppercase"},
		{"a_b", "underscore"},
		{strings.Repeat("界", 30), "not allowed"},
		{"a—b", "em dash"},
		{"one two", "space"},
		{strings.Repeat("x", 81), "limit is 80"},
	}
	for _, tc := range cases {
		if _, err := Validate(tc.value); err == nil || !strings.Contains(err.Error(), tc.message) {
			t.Errorf("Validate(%q) = %v, want message containing %q", tc.value, err, tc.message)
		}
	}
}

func TestProject(t *testing.T) {
	if _, err := Project("  notes  "); err != nil {
		t.Fatalf("Project: %v", err)
	}
	for _, value := range []string{"", "Mixed", "two words", "a/b", strings.Repeat("x", 41)} {
		if _, err := Project(value); err == nil {
			t.Errorf("Project(%q) accepted", value)
		}
	}
}

func TestFromPath(t *testing.T) {
	for input, want := range map[string]string{
		`ops\boot.md`:  "ops/boot",
		"./notes/a.MD": "notes/a",
		"plain":        "plain",
	} {
		if got := FromPath(input); got != want {
			t.Errorf("FromPath(%q) = %q, want %q", input, got, want)
		}
	}
}
