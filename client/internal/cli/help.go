package cli

import (
	_ "embed"
	"encoding/json"
	"fmt"
	"os"
	"strings"

	"github.com/BCCC0/spark-memory/client/internal/memerr"
)

//go:embed help.json
var helpJSON []byte

func helpEntries() map[string]string {
	var entries map[string]string
	_ = json.Unmarshal(helpJSON, &entries)
	return entries
}
func usageFor(key string) string {
	key = strings.ReplaceAll(key, ":", " ")
	content := helpEntries()[key]
	if content == "" {
		content = helpEntries()["root"]
	}
	return strings.TrimSpace(strings.SplitN(content, "\n\n", 2)[0])
}
func usageError(key, message string) error {
	return memerr.New("usage_error", message, map[string]any{"usage": usageFor(key)})
}

func printHelp(o options) {
	entries := helpEntries()
	key := o.command
	if !o.explicit {
		key = "root"
	}
	if o.sub != "" {
		key += " " + o.sub
	}
	if key == "" {
		key = "root"
	}
	if content, ok := entries[key]; ok {
		fmt.Fprint(os.Stdout, content)
	} else {
		fmt.Fprint(os.Stdout, entries["root"])
	}
}
