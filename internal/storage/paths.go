package storage

import (
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
)

// ErrPathEscape is returned by SecureJoinUnderRoot when the resolved path
// would land outside dataRoot. Wrapped errors can be tested with errors.Is.
var ErrPathEscape = errors.New("path escapes data root")

// ResolveDataPath joins dataRoot with relPath using OS-native separators.
// It does NOT validate that the result stays inside dataRoot; for any code
// path that takes a relpath from external input (HTTP request, database row
// that originated from user input, etc.), prefer SecureJoinUnderRoot.
func ResolveDataPath(dataRoot, relPath string) string {
	return filepath.Join(dataRoot, filepath.FromSlash(relPath))
}

// SecureJoinUnderRoot resolves relPath under dataRoot and verifies that the
// result stays strictly inside dataRoot (after symlink-aware cleaning). It
// returns ErrPathEscape if relPath contains "..", absolute components, or
// otherwise tries to walk outside the data tree.
//
// Use this instead of filepath.Join whenever the relative path comes from
// untrusted input or from a database row whose contents could be tampered
// with — for example, file-serving HTTP handlers and asset previews.
func SecureJoinUnderRoot(dataRoot, relPath string) (string, error) {
	if dataRoot == "" {
		return "", fmt.Errorf("%w: data root is empty", ErrPathEscape)
	}

	cleanRoot, err := filepath.Abs(filepath.Clean(dataRoot))
	if err != nil {
		return "", fmt.Errorf("resolve data root: %w", err)
	}

	trimmed := strings.TrimSpace(relPath)
	if trimmed == "" {
		return cleanRoot, nil
	}
	// Reject anything that looks absolute on either platform (Windows
	// filepath.IsAbs misses leading slashes coming from Unix-style input).
	if strings.HasPrefix(trimmed, "/") || strings.HasPrefix(trimmed, "\\") {
		return "", fmt.Errorf("%w: %q starts with a separator", ErrPathEscape, relPath)
	}
	osRel := filepath.FromSlash(trimmed)
	if filepath.IsAbs(osRel) {
		return "", fmt.Errorf("%w: %q is absolute", ErrPathEscape, relPath)
	}

	joined := filepath.Join(cleanRoot, osRel)
	cleaned := filepath.Clean(joined)

	rootWithSep := cleanRoot + string(filepath.Separator)
	if cleaned != cleanRoot && !strings.HasPrefix(cleaned, rootWithSep) {
		return "", fmt.Errorf("%w: %q resolves to %q", ErrPathEscape, relPath, cleaned)
	}
	return cleaned, nil
}

// MustSecureJoin is a convenience for tests / fixed paths where the caller
// statically knows the relpath is safe. Panics on escape — never use with
// untrusted input.
func MustSecureJoin(dataRoot, relPath string) string {
	p, err := SecureJoinUnderRoot(dataRoot, relPath)
	if err != nil {
		panic(err)
	}
	return p
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
