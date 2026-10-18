package hostinfo

import (
	"runtime"
	"testing"
)

func TestMachineMatchesPythonSpelling(t *testing.T) {
	got := Machine()
	if got == "" {
		t.Fatal("Machine() is empty")
	}
	if runtime.GOARCH == "amd64" && runtime.GOOS != "windows" && got != "x86_64" {
		t.Fatalf("Machine() = %q on %s/amd64, want x86_64", got, runtime.GOOS)
	}
	if runtime.GOARCH == "amd64" && runtime.GOOS == "windows" && got != "AMD64" {
		t.Fatalf("Machine() = %q on windows/amd64, want AMD64", got)
	}
}
