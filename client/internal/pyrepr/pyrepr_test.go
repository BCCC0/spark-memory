package pyrepr

import "testing"

func TestStringMatchesPythonRepr(t *testing.T) {
	cases := map[string]string{
		"hello":  "'hello'",
		"a'b":    `"a'b"`,
		"a\"b":   `'a"b'`,
		"a\nb":   `'a\nb'`,
		"\u00a0": `'\xa0'`,
		"\u2028": `'\u2028'`,
		"界":      "'界'",
	}
	for value, want := range cases {
		if got := String(value); got != want {
			t.Errorf("String(%q) = %s, want %s", value, got, want)
		}
	}
}
