package pointers

import "testing"

func TestParseCanonical(t *testing.T) {
	for _, value := range []string{
		"@file(laptop:C:\\src\\x)",
		"@dir(home:/docs)",
		"@locked(home:/secret)",
	} {
		if _, err := Parse(value, false, ""); err != nil {
			t.Errorf("Parse(%q): %v", value, err)
		}
	}
	p, err := Parse("@file(laptop:C:\\src\\x)", false, "")
	if err != nil {
		t.Fatal(err)
	}
	if p.Canonical() != "@file(laptop:C:\\src\\x)" {
		t.Errorf("Canonical = %q", p.Canonical())
	}
	for _, value := range []string{
		"@file(no_alias:/a)",
		"@file(home:/a)b)",
		"@file(home:)",
		"@file(self:/a)",
		"laptop:/a",
	} {
		if _, err := Parse(value, false, ""); err == nil {
			t.Errorf("Parse(%q) accepted", value)
		}
	}
}

func TestParseUnqualified(t *testing.T) {
	p, err := Parse("@file(C:/x)", true, "")
	if err != nil {
		t.Fatal(err)
	}
	if p.Alias != "self" || p.Path != "C:/x" {
		t.Fatalf("unqualified parse = %#v", p)
	}
	if _, err := Parse("@file(self:/a)", false, ""); err == nil {
		t.Fatal("self alias accepted outside a device home")
	}
}
