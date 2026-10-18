package pyrepr

import (
	"fmt"
	"strings"
	"unicode"
)

// String renders a string like Python's repr for user-facing validation errors.
func String(value string) string {
	quote := '\''
	if strings.ContainsRune(value, '\'') && !strings.ContainsRune(value, '"') {
		quote = '"'
	}
	var out strings.Builder
	out.WriteRune(quote)
	for _, r := range value {
		switch r {
		case '\\':
			out.WriteString("\\\\")
		case '\n':
			out.WriteString("\\n")
		case '\r':
			out.WriteString("\\r")
		case '\t':
			out.WriteString("\\t")
		default:
			if r == quote {
				out.WriteByte('\\')
			}
			if !unicode.IsPrint(r) {
				switch {
				case r < 256:
					fmt.Fprintf(&out, "\\x%02x", r)
				case r < 65536:
					fmt.Fprintf(&out, "\\u%04x", r)
				default:
					fmt.Fprintf(&out, "\\U%08x", r)
				}
			} else {
				out.WriteRune(r)
			}
		}
	}
	out.WriteRune(quote)
	return out.String()
}
