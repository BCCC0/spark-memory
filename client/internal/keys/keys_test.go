package keys

import "testing"

func TestKeysMatchPythonOracle(t *testing.T) {
	if got := Add("abc", "n", "rev", "D:/x", "change"); got != "abc:n:rev:e18f0aa3b63815d4a09429a0a882815b596c170a0d1af8af28b48af480147167:add" {
		t.Errorf("Add = %q", got)
	}
	if got := Add("abc", "n", "", "", ""); got != "abc:n::6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d:add" {
		t.Errorf("Add empty = %q", got)
	}
	if got := Remove("n", "rev", "change"); got != "n:rev:12ea12eace7d655f471ce55e34f89b1b77a3d9d05a445ca82877dd2235beaa51:rm" {
		t.Errorf("Remove = %q", got)
	}
	if got := Remove("n", "", ""); got != "n::e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855:rm" {
		t.Errorf("Remove empty = %q", got)
	}
	if got := Move("a", "b", "head", "move"); got != "a:b:head:683a62ce15fbabb1ac867022e56e6e4f4f581762d77c3abb2f8dc8b165b3b1b9:mv" {
		t.Errorf("Move = %q", got)
	}
}
