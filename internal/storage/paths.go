package storage

import (
	"fmt"
	"os"
	"path/filepath"
)

func ResolveDataPath(dataRoot, relPath string) string {
	return filepath.Join(dataRoot, filepath.FromSlash(relPath))
}

func EnsureParentDir(path string) error {
	parent := filepath.Dir(path)
	if parent == "." || parent == "" {
		return nil
	}
	if err := os.MkdirAll(parent, 0o755); err != nil {
		return fmt.Errorf("create parent directory %s: %w", parent, err)
	}
	return nil
}

func EnsureDataDirs(dataRoot string) error {
	return os.MkdirAll(dataRoot, 0o755)
}
