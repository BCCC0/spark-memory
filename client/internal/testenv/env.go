package testenv

import (
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// BinaryPath is set once by the consuming package's TestMain.
var BinaryPath string

type Env struct {
	T          *testing.T
	Daemon     *Daemon
	Home       string
	ConfigDir  string
	ProjectDir string
	WorkDir    string
	Binary     string
	Vars       []string
}

type Result struct {
	Stdout, Stderr string
	Exit           int
	JSON           map[string]any
	Error          map[string]any
}

func New(t *testing.T) *Env {
	t.Helper()
	if BinaryPath == "" {
		t.Fatal("testenv.BinaryPath is not set; add a TestMain that builds the binary")
	}
	home := t.TempDir()
	work := t.TempDir()
	configDir := filepath.Join(home, ".config", "mem")
	if err := os.MkdirAll(configDir, 0700); err != nil {
		t.Fatal(err)
	}
	env := &Env{T: t, Daemon: NewDaemon(t), Home: home, ConfigDir: configDir, ProjectDir: work, WorkDir: work, Binary: BinaryPath}
	env.Vars = append(os.Environ(),
		"MEM_CONFIG_DIR="+configDir,
		"HOME="+home,
		"USERPROFILE="+home,
		"MEM_ENROLL_SECRET=test-secret",
	)
	env.Write(".mem.toml", "project = \"demo\"\n")
	return env
}

func (e *Env) Write(relPath, content string) string {
	e.T.Helper()
	target := filepath.Join(e.WorkDir, filepath.FromSlash(relPath))
	if err := os.MkdirAll(filepath.Dir(target), 0755); err != nil {
		e.T.Fatal(err)
	}
	if err := os.WriteFile(target, []byte(content), 0644); err != nil {
		e.T.Fatal(err)
	}
	return target
}

func (e *Env) Enroll(alias string) {
	e.T.Helper()
	content := fmt.Sprintf("server = %q\ndevice_id = \"d1\"\ndisplay_name = \"test-device\"\nalias = %q\ntoken = \"token\"\n", e.Daemon.URL, alias)
	if err := os.WriteFile(filepath.Join(e.ConfigDir, "credentials"), []byte(content), 0600); err != nil {
		e.T.Fatal(err)
	}
}

func (e *Env) Mem(args ...string) Result { return e.MemIn(e.WorkDir, args...) }

func (e *Env) MemIn(dir string, args ...string) Result {
	e.T.Helper()
	command := exec.Command(e.Binary, args...)
	command.Dir = dir
	command.Env = e.Vars
	var stdout, stderr strings.Builder
	command.Stdout = &stdout
	command.Stderr = &stderr
	err := command.Run()
	result := Result{Stdout: stdout.String(), Stderr: stderr.String()}
	if err != nil {
		if exit, ok := err.(*exec.ExitError); ok {
			result.Exit = exit.ExitCode()
		} else {
			e.T.Fatalf("run %v: %v", args, err)
		}
	}
	_ = json.Unmarshal([]byte(result.Stdout), &result.JSON)
	_ = json.Unmarshal([]byte(result.Stderr), &result.Error)
	return result
}

func (e *Env) MustMem(args ...string) Result {
	e.T.Helper()
	result := e.Mem(args...)
	if result.Exit != 0 {
		e.T.Fatalf("mem %v exited %d\nstdout=%s\nstderr=%s", args, result.Exit, result.Stdout, result.Stderr)
	}
	return result
}

func writeJSON(w interface{ Write([]byte) (int, error) }, value any) {
	data, _ := json.Marshal(value)
	_, _ = w.Write(data)
}
