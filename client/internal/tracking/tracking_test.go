package tracking

import "testing"

func TestPatternSemantics(t *testing.T) {
	cases := []struct {
		patterns []string
		path     string
		want     bool
	}{
		{[]string{"*.md"}, "deep/a.md", true},
		{[]string{"notes/*.md"}, "notes/a.md", true},
		{[]string{"notes/*.md"}, "notes/deep/a.md", false},
		{[]string{"docs/**/*.md"}, "docs/a.md", true},
		{[]string{"docs/**/*.md"}, "docs/sub/a.md", true},
		{[]string{"*.md", "!private/"}, "private/a.md", false},
		{[]string{"file[!0-9].md"}, "filex.md", true},
		{[]string{"file[!0-9].md"}, "file1.md", false},
		{[]string{"file[0-9].md"}, "file7.md", true},
		{[]string{"a/./b/*.md"}, "a/b/x.md", true},
		{[]string{"?"}, "x", true},
		{[]string{}, "a.md", false},
	}
	for _, c := range cases {
		if got := Matches(c.patterns, c.path); got != c.want {
			t.Errorf("%v matches %q: got %v, want %v", c.patterns, c.path, got, c.want)
		}
	}
}
