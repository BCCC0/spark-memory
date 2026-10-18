package cli

import (
	"os/exec"
	"strings"
	"testing"

	"github.com/BCCC0/spark-memory/client/internal/testenv"
)

func TestVersionFlagReportsGoClient(t *testing.T) {
	out, err := exec.Command(testenv.BinaryPath, "--version").CombinedOutput()
	if err != nil {
		t.Fatalf("mem --version: %v\n%s", err, out)
	}
	text := strings.TrimSpace(string(out))
	if !strings.HasPrefix(text, "mem ") {
		t.Fatalf("version output = %q", text)
	}
	if !strings.Contains(text, "(go, commit ") || !strings.Contains(text, ")") {
		t.Fatalf("version output does not identify the implementation and commit: %q", text)
	}
}
