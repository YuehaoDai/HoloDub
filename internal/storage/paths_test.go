package storage

import (
	"errors"
	"path/filepath"
	"strings"
	"testing"
)

func TestSecureJoinUnderRoot(t *testing.T) {
	root := t.TempDir()

	tests := []struct {
		name      string
		relPath   string
		wantErr   bool
		errIs     error
		wantRel   string // expected suffix relative to root (using OS separators)
		wantEqual bool   // if true the result must equal the cleaned root
	}{
		{
			name:      "empty relpath returns root",
			relPath:   "",
			wantEqual: true,
		},
		{
			name:    "simple posix relpath",
			relPath: "jobs/12/output/final.mp4",
			wantRel: filepath.Join("jobs", "12", "output", "final.mp4"),
		},
		{
			name:    "windows separators normalised",
			relPath: "jobs\\12\\output.mp4",
			wantRel: filepath.Join("jobs", "12", "output.mp4"),
		},
		{
			name:    "dot segments resolved inside root",
			relPath: "jobs/./12/./output.mp4",
			wantRel: filepath.Join("jobs", "12", "output.mp4"),
		},
		{
			name:    "nested dotdot stays inside root",
			relPath: "jobs/12/../11/output.mp4",
			wantRel: filepath.Join("jobs", "11", "output.mp4"),
		},
		{
			name:    "balanced dotdot resolves inside root",
			relPath: "jobs/12/../../etc/passwd",
			wantRel: filepath.Join("etc", "passwd"),
		},
		{
			name:    "leading dotdot escapes",
			relPath: "../etc/passwd",
			wantErr: true,
			errIs:   ErrPathEscape,
		},
		{
			name:    "leading dotdot deep escape",
			relPath: "../../../../../../etc/passwd",
			wantErr: true,
			errIs:   ErrPathEscape,
		},
		{
			name:    "leading slash rejected (posix-style absolute)",
			relPath: "/etc/passwd",
			wantErr: true,
			errIs:   ErrPathEscape,
		},
		{
			name:    "leading backslash rejected (windows-style absolute)",
			relPath: "\\etc\\passwd",
			wantErr: true,
			errIs:   ErrPathEscape,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := SecureJoinUnderRoot(root, tt.relPath)
			if tt.wantErr {
				if err == nil {
					t.Fatalf("expected error, got %q", got)
				}
				if tt.errIs != nil && !errors.Is(err, tt.errIs) {
					t.Fatalf("expected errors.Is(err, %v), got %v", tt.errIs, err)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			cleanRoot, _ := filepath.Abs(filepath.Clean(root))
			if tt.wantEqual {
				if got != cleanRoot {
					t.Fatalf("expected %q, got %q", cleanRoot, got)
				}
				return
			}
			want := filepath.Join(cleanRoot, tt.wantRel)
			if got != want {
				t.Fatalf("expected %q, got %q", want, got)
			}
			// Stricter invariant: result must be inside the cleaned root.
			if !strings.HasPrefix(got, cleanRoot+string(filepath.Separator)) && got != cleanRoot {
				t.Fatalf("result %q is not under root %q", got, cleanRoot)
			}
		})
	}
}

func TestSecureJoinUnderRoot_EmptyRoot(t *testing.T) {
	if _, err := SecureJoinUnderRoot("", "foo"); err == nil {
		t.Fatal("expected error for empty root")
	}
}

func TestMustSecureJoin_PanicsOnEscape(t *testing.T) {
	defer func() {
		if r := recover(); r == nil {
			t.Fatal("expected panic")
		}
	}()
	MustSecureJoin(t.TempDir(), "../escape")
}
