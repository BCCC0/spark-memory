package cli

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/BCCC0/spark-memory/client/internal/testenv"
)

func TestMain(m *testing.M) {
	dir, err := os.MkdirTemp("", "mem-bin")
	if err != nil {
		panic(err)
	}
	name := "mem-go"
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	binary := filepath.Join(dir, name)
	build := exec.Command("go", "build", "-o", binary, "../../cmd/mem")
	build.Stderr = os.Stderr
	if err := build.Run(); err != nil {
		panic(fmt.Sprintf("build mem binary: %v", err))
	}
	testenv.BinaryPath = binary
	code := m.Run()
	_ = os.RemoveAll(dir)
	os.Exit(code)
}
