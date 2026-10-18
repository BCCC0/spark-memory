package pointers

import (
	"regexp"
	"strings"

	"github.com/BCCC0/spark-memory/client/internal/memerr"
	"github.com/BCCC0/spark-memory/client/internal/pyrepr"
)

type Pointer struct{ Kind, Alias, Path string }

func (p Pointer) Canonical() string { return "@" + p.Kind + "(" + p.Alias + ":" + p.Path + ")" }

var aliasPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9-]{0,39}$`)

func Parse(value string, allowUnqualified bool, selfAlias string) (Pointer, error) {
	var p Pointer
	value = strings.TrimSpace(value)
	for _, kind := range []string{"file", "dir", "locked"} {
		if strings.HasPrefix(value, "@"+kind+"(") && strings.HasSuffix(value, ")") {
			p.Kind = kind
			value = value[len(kind)+2 : len(value)-1]
			break
		}
	}
	if p.Kind == "" {
		return p, memerr.New("schema_invalid", "pointer must use @file(alias:path), @dir(alias:path), or @locked(alias:path)", nil)
	}
	if i := strings.Index(value, ":"); i >= 0 && !(allowUnqualified && i == 1 && len(value) > 2 && (value[2] == '/' || value[2] == '\\')) {
		if i == len(value)-1 || strings.ContainsAny(value[:i], "()") {
			return p, memerr.New("schema_invalid", "pointer must use @file(alias:path), @dir(alias:path), or @locked(alias:path)", nil)
		}
		p.Alias, p.Path = value[:i], value[i+1:]
	} else if allowUnqualified {
		p.Alias, p.Path = selfAlias, value
		if p.Alias == "" {
			p.Alias = "self"
		}
	} else {
		return p, memerr.New("schema_invalid", "project pointers require a device alias: @file(alias:path)", nil)
	}
	if !aliasPattern.MatchString(p.Alias) {
		return p, memerr.New("schema_invalid", "invalid device alias "+pyrepr.String(p.Alias), nil)
	}
	if p.Alias == "self" && !allowUnqualified {
		return p, memerr.New("schema_invalid", "'self' is only valid inside a device home; project pointers need an enrolled alias", nil)
	}
	if p.Path == "" || strings.ContainsAny(p.Path, "\r\n)") {
		return p, memerr.New("schema_invalid", "pointer path must be a non-empty single line without ')'", nil)
	}
	return p, nil
}
