package keys

import (
	"crypto/sha256"
	"encoding/hex"
	"strings"
)

func hash(values ...string) string {
	sum := sha256.Sum256([]byte(strings.Join(values, "\x00")))
	return hex.EncodeToString(sum[:])
}
func Add(content, path, expect, origin, message string) string {
	return content + ":" + path + ":" + expect + ":" + hash(origin, message) + ":add"
}
func Remove(path, expect, message string) string {
	return path + ":" + expect + ":" + hash(message) + ":rm"
}
func Move(source, destination, expect, message string) string {
	return source + ":" + destination + ":" + expect + ":" + hash(message) + ":mv"
}
