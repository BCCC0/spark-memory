package summary

import (
	"strings"
	"testing"
)

func TestValidateMatchesPyYAML(t *testing.T) {
	cases := []struct {
		name, summary string
		ok            bool
		want          string
	}{
		{"plain", "hello", true, "hello"},
		{"quote", `"yes"`, true, "yes"},
		{"single quote", `'on'`, true, "on"},
		{"y", "y", true, "y"},
		{"n", "n", true, "n"},
		{"lower yes", "yes", false, ""},
		{"title yes", "Yes", false, ""},
		{"upper yes", "YES", false, ""},
		{"mixed yes", "yEs", true, "yEs"},
		{"lower on", "on", false, ""},
		{"mixed on", "oN", true, "oN"},
		{"off", "Off", false, ""},
		{"mixed off", "oFf", true, "oFf"},
		{"true", "true", false, ""},
		{"number", "123", false, ""},
		{"null", "~", false, ""},
		{"explicit string yes", "!!str yes", true, "yes"},
		{"explicit string on", "!!str on", true, "on"},
		{"explicit bool", "!!bool yes", false, ""},
		{"alias", "&a hello\nsummary: *a", true, "hello"},
		{"block", "|\n  a\n  b", false, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			text := "---\nsummary: " + tc.summary + "\n---\nbody\n"
			got, err := Validate(text)
			if tc.ok {
				if err != nil {
					t.Fatalf("Validate() error: %v", err)
				}
				if got != tc.want {
					t.Fatalf("Validate() = %q, want %q", got, tc.want)
				}
				return
			}
			if err == nil {
				t.Fatalf("Validate() = %q, want error", got)
			}
		})
	}
}

func TestValidateDuplicateAndMerge(t *testing.T) {
	cases := []struct {
		name, frontmatter, want string
		ok                      bool
	}{
		{"duplicate last wins", "summary: first\nsummary: last", "last", true},
		{"merge supplies summary", "defaults: &d\n  summary: merged\n<<: *d", "merged", true},
		{"explicit beats merge", "defaults: &d\n  summary: merged\n<<: *d\nsummary: explicit", "explicit", true},
		{"merge sequence first wins", "a: &a\n  summary: first\nb: &b\n  summary: second\n<<: [*a, *b]", "first", true},
		{"duplicate mixed type", "summary: yes\nsummary: last", "last", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got, err := Validate("---\n" + tc.frontmatter + "\n---\nbody\n")
			if tc.ok {
				if err != nil {
					t.Fatalf("Validate() error: %v", err)
				}
				if got != tc.want {
					t.Fatalf("Validate() = %q, want %q", got, tc.want)
				}
				return
			}
			if err == nil {
				t.Fatalf("Validate() = %q, want error", got)
			}
		})
	}
}

func TestValidateLengthCountsCodePoints(t *testing.T) {
	if _, err := Validate("---\nsummary: " + strings.Repeat("界", 200) + "\n---\n"); err != nil {
		t.Fatalf("200 CJK characters should pass: %v", err)
	}
	if _, err := Validate("---\nsummary: " + strings.Repeat("界", 201) + "\n---\n"); err == nil {
		t.Fatal("201 CJK characters should fail")
	}
}
