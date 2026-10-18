// Package encoding holds the language-level primitives where Go and Python
// disagree about what a "character" is. Every length gate in the client is
// written against Python's len(), which counts code points, not UTF-8 bytes.
package encoding

import "unicode/utf8"

// RuneCount is Python's len(str): the number of code points.
func RuneCount(value string) int { return utf8.RuneCountInString(value) }
