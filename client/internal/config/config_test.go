package config

import (
	"os"
	"path/filepath"
	"testing"
)

func TestServerPrecedenceAndNearestMarker(t *testing.T) {
	root := t.TempDir()
	dir := filepath.Join(root, "config")
	project := filepath.Join(root, "project")
	nested := filepath.Join(project, "nested")
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(nested, 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("MEM_CONFIG_DIR", dir)
	t.Setenv("MEM_SERVER", "http://env")
	if err := os.WriteFile(filepath.Join(dir, "config.toml"), []byte("server = 'http://file'\n[read]\nmax_bytes = '123'\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dir, "credentials"), []byte("server = 'http://credentials'\ntoken = 'secret'\ndevice_id = 'd'\n"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(project, ".mem.toml"), []byte("project = 'notes'\nserver = 'http://marker'\n"), 0600); err != nil {
		t.Fatal(err)
	}
	t.Chdir(nested)
	c, err := Load(true, true)
	if err != nil {
		t.Fatal(err)
	}
	if c.Server != "http://marker" || c.Project != "notes" || c.MaxBytes != 123 || c.Credentials.Token != "secret" {
		t.Fatalf("loaded config: %#v", c)
	}
	if server, err := LoginServer(); err != nil || server != "http://file" {
		t.Fatalf("login server = %q, %v", server, err)
	}
}

func TestMarkerAcceptsScalarAndArrayPatterns(t *testing.T) {
	for _, text := range []string{
		"project = 'p'\n[track]\npatterns = '*.md'\n",
		"project = 'p'\n[track]\npatterns = ['*.md', '!drafts/**']\n",
	} {
		marker, err := ParseMarker([]byte(text))
		if err != nil {
			t.Fatalf("ParseMarker(%q): %v", text, err)
		}
		if len(marker.Track.Patterns) == 0 {
			t.Fatalf("ParseMarker(%q) lost patterns", text)
		}
	}
}

func TestMarkerRejectsMalformedTOML(t *testing.T) {
	for _, text := range []string{
		"project = 'p'\nproject = 'q'\n",
		"project = [\n",
	} {
		if _, err := ParseMarker([]byte(text)); err == nil {
			t.Fatalf("ParseMarker(%q) accepted malformed TOML", text)
		}
	}
}
