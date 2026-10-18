package config

import (
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/BCCC0/spark-memory/client/internal/memerr"
	"github.com/BurntSushi/toml"
)

const DefaultServer = "http://127.0.0.1:8321"

type Credentials struct {
	Server      string `toml:"server"`
	DeviceID    string `toml:"device_id"`
	Device      string `toml:"device"`
	DisplayName string `toml:"display_name"`
	Alias       string `toml:"alias"`
	Token       string `toml:"token"`
}
type Config struct {
	Server      string
	MaxBytes    int
	Credentials *Credentials
	Project     string
	MarkerPath  string
}
type configFile struct {
	Server string `toml:"server"`
	Read   struct {
		MaxBytes FlexInt `toml:"max_bytes"`
	} `toml:"read"`
}

// PatternList accepts both forms the Python client accepts:
//
//	patterns = ["*.md"]
//	patterns = "*.md"
type PatternList []string

func (p *PatternList) UnmarshalTOML(value any) error {
	switch v := value.(type) {
	case string:
		*p = PatternList{v}
		return nil
	case []any:
		out := make(PatternList, 0, len(v))
		for _, item := range v {
			out = append(out, fmt.Sprint(item))
		}
		*p = out
		return nil
	case []string:
		*p = PatternList(v)
		return nil
	default:
		return fmt.Errorf("patterns must be a string or an array of strings")
	}
}

// FlexInt accepts an integer or a numeric string. Python's config loader calls
// int() on the value, so "4096" is valid there and must be here too.
type FlexInt int

func (f *FlexInt) UnmarshalTOML(value any) error {
	switch v := value.(type) {
	case int64:
		*f = FlexInt(v)
		return nil
	case int:
		*f = FlexInt(v)
		return nil
	case float64:
		*f = FlexInt(int(v))
		return nil
	case string:
		n, err := strconv.Atoi(v)
		if err != nil {
			return fmt.Errorf("max_bytes must be an integer: %w", err)
		}
		*f = FlexInt(n)
		return nil
	default:
		return fmt.Errorf("max_bytes must be an integer")
	}
}

type Marker struct {
	Project string `toml:"project"`
	Server  string `toml:"server"`
	Track   struct {
		Patterns PatternList `toml:"patterns"`
	} `toml:"track"`
}

// ParseMarker parses a project marker without touching the filesystem.
func ParseMarker(data []byte) (Marker, error) {
	var marker Marker
	if _, err := toml.Decode(string(data), &marker); err != nil {
		return marker, err
	}
	return marker, nil
}

func Dir() string {
	if v := os.Getenv("MEM_CONFIG_DIR"); v != "" {
		return v
	}
	home := os.Getenv("HOME")
	if home == "" {
		home = os.Getenv("USERPROFILE")
	}
	if home == "" {
		home, _ = os.UserHomeDir()
	}
	return filepath.Join(home, ".config", "mem")
}
func read(path string, value any) error {
	if _, err := os.Stat(path); os.IsNotExist(err) {
		return nil
	} else if err != nil {
		return memerr.New("internal_error", "cannot read "+path+": "+err.Error(), nil)
	}
	if _, err := toml.DecodeFile(path, value); err != nil {
		return memerr.New("invalid_request", fmt.Sprintf("%s is not valid TOML: %v", path, err), nil)
	}
	return nil
}
func ReadCredentials() (*Credentials, error) {
	path := filepath.Join(Dir(), "credentials")
	if _, err := os.Stat(path); os.IsNotExist(err) {
		return nil, nil
	}
	c := new(Credentials)
	if err := read(path, c); err != nil {
		return nil, err
	}
	if c.Server == "" || c.Token == "" {
		return nil, memerr.New("unauthorized", path+" is missing server or token — run `mem login` again", nil)
	}
	if c.DeviceID == "" {
		c.DeviceID = c.Device
	}
	return c, nil
}
func LoginServer() (string, error) {
	var file configFile
	if err := read(filepath.Join(Dir(), "config.toml"), &file); err != nil {
		return "", err
	}
	if file.Server != "" {
		return file.Server, nil
	}
	return DefaultServer, nil
}
func WriteCredentials(c Credentials) (string, error) {
	path := filepath.Join(Dir(), "credentials")
	if err := os.MkdirAll(filepath.Dir(path), 0700); err != nil {
		return "", memerr.New("internal_error", err.Error(), nil)
	}
	var b strings.Builder
	for _, item := range [][2]string{{"server", c.Server}, {"device_id", c.DeviceID}, {"display_name", c.DisplayName}, {"alias", c.Alias}, {"token", c.Token}} {
		fmt.Fprintf(&b, "%s = %s\n", item[0], strconv.Quote(item[1]))
	}
	if err := writePrivate(path, []byte(b.String())); err != nil {
		return "", memerr.New("internal_error", err.Error(), nil)
	}
	return path, nil
}

// writePrivate writes a secret file and tightens an existing file's mode.
// os.WriteFile's perm only applies on creation, so a pre-existing 0644
// credentials file would otherwise stay world-readable after a re-login.
func writePrivate(path string, data []byte) error {
	file, err := os.OpenFile(path, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0600)
	if err != nil {
		return err
	}
	_ = file.Chmod(0600)
	if _, err := file.Write(data); err != nil {
		file.Close()
		return err
	}
	return file.Close()
}
func DeleteCredentials() (bool, error) {
	err := os.Remove(filepath.Join(Dir(), "credentials"))
	if os.IsNotExist(err) {
		return false, nil
	}
	if err != nil {
		return false, memerr.New("internal_error", err.Error(), nil)
	}
	return true, nil
}
func WriteConfig(server string) error {
	if err := os.MkdirAll(Dir(), 0700); err != nil {
		return memerr.New("internal_error", err.Error(), nil)
	}
	data := fmt.Sprintf("server = %s\n[read]\nmax_bytes = 4096\n", strconv.Quote(server))
	if err := writePrivate(filepath.Join(Dir(), "config.toml"), []byte(data)); err != nil {
		return memerr.New("internal_error", err.Error(), nil)
	}
	return nil
}
func FindMarker(start string) (string, Marker, error) {
	var marker Marker
	current, err := filepath.Abs(start)
	if err != nil {
		return "", marker, err
	}
	for {
		path := filepath.Join(current, ".mem.toml")
		if data, err := os.ReadFile(path); err == nil {
			marker, err := ParseMarker(data)
			if err != nil {
				return "", marker, memerr.New("invalid_request", fmt.Sprintf("%s is not valid TOML: %v", path, err), nil)
			}
			return path, marker, nil
		} else if !os.IsNotExist(err) {
			return "", marker, err
		}
		parent := filepath.Dir(current)
		if parent == current {
			return "", marker, nil
		}
		current = parent
	}
}
func WriteMarker(dir, project string) (string, error) {
	path := filepath.Join(dir, ".mem.toml")
	if err := os.WriteFile(path, []byte("project = "+strconv.Quote(project)+"\n"), 0644); err != nil {
		return "", memerr.New("internal_error", err.Error(), nil)
	}
	return path, nil
}
func WritePatterns(path string, patterns []string) error {
	data, err := os.ReadFile(path)
	if err != nil {
		return memerr.New("internal_error", err.Error(), nil)
	}
	lines := strings.Split(string(data), "\n")
	start, end := -1, len(lines)
	for i, line := range lines {
		if strings.TrimSpace(line) == "[track]" {
			start = i
			for j := i + 1; j < len(lines); j++ {
				v := strings.TrimSpace(lines[j])
				if strings.HasPrefix(v, "[") && strings.HasSuffix(v, "]") {
					end = j
					break
				}
			}
			break
		}
	}
	replacement := []string{}
	if len(patterns) > 0 {
		values := []string{}
		for _, p := range patterns {
			values = append(values, strconv.Quote(p))
		}
		replacement = []string{"[track]", "patterns = [" + strings.Join(values, ", ") + "]"}
	}
	if start >= 0 {
		lines = append(append(append([]string{}, lines[:start]...), replacement...), lines[end:]...)
	} else if len(replacement) > 0 {
		lines = append(lines, replacement...)
	}
	text := strings.TrimRight(strings.Join(lines, "\n"), "\n") + "\n"
	if err := os.WriteFile(path, []byte(text), 0644); err != nil {
		return memerr.New("internal_error", err.Error(), nil)
	}
	return nil
}
func Load(requireProject, requireAuth bool) (Config, error) {
	c := Config{Server: DefaultServer, MaxBytes: 4096}
	var file configFile
	if err := read(filepath.Join(Dir(), "config.toml"), &file); err != nil {
		return c, err
	}
	if file.Server != "" {
		c.Server = file.Server
	}
	if file.Read.MaxBytes != 0 {
		c.MaxBytes = int(file.Read.MaxBytes)
	}
	if value := os.Getenv("MEM_SERVER"); value != "" {
		c.Server = value
	}
	creds, err := ReadCredentials()
	if err != nil {
		return c, err
	}
	c.Credentials = creds
	if creds != nil {
		c.Server = creds.Server
	}
	wd, err := os.Getwd()
	if err != nil {
		return c, err
	}
	markerPath, marker, err := FindMarker(wd)
	if err != nil {
		return c, err
	}
	c.MarkerPath, c.Project = markerPath, marker.Project
	if marker.Server != "" {
		c.Server = marker.Server
	}
	if requireProject && c.Project == "" {
		return c, memerr.New("no_project", "no .mem.toml found in this directory or any parent", map[string]any{"hint": "run `mem init <project>` here"})
	}
	if requireAuth && creds == nil {
		return c, memerr.New("unauthorized", "this device is not enrolled", map[string]any{"hint": "run `mem login`"})
	}
	return c, nil
}
