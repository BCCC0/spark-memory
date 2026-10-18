package summary

import (
	"fmt"
	"regexp"
	"strings"
	"unicode/utf8"

	"github.com/BCCC0/spark-memory/client/internal/encoding"
	"github.com/BCCC0/spark-memory/client/internal/memerr"
	"gopkg.in/yaml.v3"
)

var delimiter = regexp.MustCompile(`^---\s*$`)

// PyYAML's YAML 1.1 resolver accepts exactly these casings for the legacy
// boolean words. It does not accept mixed-case spellings such as "yEs", and it
// leaves "y"/"n" as strings, so neither may be overridden here.
var oldBool = regexp.MustCompile(`^(?:yes|Yes|YES|no|No|NO|on|On|ON|off|Off|OFF)$`)

func Validate(text string) (string, error) {
	if !utf8.ValidString(text) {
		return "", memerr.New("schema_invalid", "Markdown is not UTF-8", nil)
	}
	lines := strings.Split(strings.ReplaceAll(text, "\r\n", "\n"), "\n")
	if len(lines) == 0 || !delimiter.MatchString(lines[0]) {
		return "", memerr.New("schema_invalid", "'summary' is required and must be a non-empty string", nil)
	}
	end := -1
	for i := 1; i < len(lines); i++ {
		if delimiter.MatchString(lines[i]) {
			end = i
			break
		}
	}
	if end < 0 {
		return "", memerr.New("schema_invalid", "frontmatter opened with '---' but was never closed", nil)
	}
	raw := strings.Join(lines[1:end], "\n")
	var doc yaml.Node
	if err := yaml.Unmarshal([]byte(raw), &doc); err != nil {
		return "", memerr.New("schema_invalid", "frontmatter is not valid YAML: "+err.Error(), nil)
	}
	if len(doc.Content) == 0 {
		return "", memerr.New("schema_invalid", "'summary' is required and must be a non-empty string", nil)
	}
	root := doc.Content[0]
	if root.Kind != yaml.MappingNode {
		return "", memerr.New("schema_invalid", "frontmatter must be a mapping", nil)
	}
	found := resolveMapping(root)["summary"]
	value, ok := stringValue(found)
	if !ok || strings.TrimSpace(value) == "" {
		return "", memerr.New("schema_invalid", "'summary' is required and must be a non-empty string", nil)
	}
	if strings.ContainsAny(value, "\r\n") {
		return "", memerr.New("schema_invalid", "'summary' must not contain a newline", nil)
	}
	if n := encoding.RuneCount(value); n > 200 {
		return "", memerr.New("schema_invalid", fmt.Sprintf("'summary' is %d chars; the limit is 200", n), nil)
	}
	return value, nil
}

// resolveMapping returns a mapping's resolved keys. Duplicate keys keep the
// last occurrence, matching PyYAML. YAML merge keys ("<<") contribute entries
// only where an explicit key has not already supplied one; among several merge
// sources the first wins, as in PyYAML.
func resolveMapping(node *yaml.Node) map[string]*yaml.Node {
	out := map[string]*yaml.Node{}
	node = deref(node)
	if node == nil || node.Kind != yaml.MappingNode {
		return out
	}
	var merges []*yaml.Node
	for i := 0; i+1 < len(node.Content); i += 2 {
		key, value := node.Content[i], node.Content[i+1]
		if key.Value == "<<" {
			merges = append(merges, value)
			continue
		}
		out[key.Value] = value
	}
	for _, merge := range merges {
		for _, source := range mergeSources(merge) {
			for key, value := range resolveMapping(source) {
				if _, exists := out[key]; !exists {
					out[key] = value
				}
			}
		}
	}
	return out
}

func mergeSources(node *yaml.Node) []*yaml.Node {
	node = deref(node)
	if node == nil {
		return nil
	}
	if node.Kind == yaml.SequenceNode {
		var sources []*yaml.Node
		for _, item := range node.Content {
			sources = append(sources, mergeSources(item)...)
		}
		return sources
	}
	if node.Kind == yaml.MappingNode {
		return []*yaml.Node{node}
	}
	return nil
}

func deref(node *yaml.Node) *yaml.Node {
	for i := 0; node != nil && node.Kind == yaml.AliasNode && node.Alias != nil && i < 100; i++ {
		node = node.Alias
	}
	if node != nil && node.Kind == yaml.AliasNode {
		return nil
	}
	return node
}

// stringValue reports whether node is a string under PyYAML's rules. A plain,
// implicitly tagged scalar that spells a legacy YAML 1.1 boolean is not a
// string even though yaml.v3 resolves it as !!str. An explicitly tagged or
// quoted scalar is a string regardless of its spelling.
func stringValue(node *yaml.Node) (string, bool) {
	node = deref(node)
	if node == nil || node.Kind != yaml.ScalarNode || node.Tag != "!!str" {
		return "", false
	}
	if node.Style&yaml.TaggedStyle != 0 {
		return node.Value, true
	}
	if node.Style&(yaml.DoubleQuotedStyle|yaml.SingleQuotedStyle|yaml.LiteralStyle|yaml.FoldedStyle) != 0 {
		return node.Value, true
	}
	if oldBool.MatchString(node.Value) {
		return "", false
	}
	return node.Value, true
}
