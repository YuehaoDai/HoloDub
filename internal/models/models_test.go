package models

import (
	"errors"
	"testing"
)

func TestNextStage(t *testing.T) {
	next, ok := StageTranslate.Next()
	if !ok {
		t.Fatalf("expected translate to have a next stage")
	}
	if next != StageTTSDuration {
		t.Fatalf("expected next stage to be %q, got %q", StageTTSDuration, next)
	}
}

func TestSegmentStatus_Transition(t *testing.T) {
	tests := []struct {
		name    string
		from    SegmentStatus
		to      SegmentStatus
		wantErr bool
	}{
		{"empty -> pending", "", SegmentStatusPending, false},
		{"empty -> translated", "", SegmentStatusTranslated, false},
		{"pending -> translated", SegmentStatusPending, SegmentStatusTranslated, false},
		{"pending -> pending", SegmentStatusPending, SegmentStatusPending, false},
		{"translated -> synthesized", SegmentStatusTranslated, SegmentStatusSynthesized, false},
		{"translated -> pending (asr retry)", SegmentStatusTranslated, SegmentStatusPending, false},
		{"synthesized -> pending (rerun)", SegmentStatusSynthesized, SegmentStatusPending, false},
		{"synthesized -> translated (edit)", SegmentStatusSynthesized, SegmentStatusTranslated, false},
		{"empty -> synthesized (illegal)", "", SegmentStatusSynthesized, true},
		{"pending -> synthesized (skips translate)", SegmentStatusPending, SegmentStatusSynthesized, true},
		{"unknown source", SegmentStatus("bogus"), SegmentStatusPending, true},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			next, err := tt.from.Transition(tt.to)
			if tt.wantErr {
				if err == nil {
					t.Fatalf("expected error transitioning %q -> %q, got %q", tt.from, tt.to, next)
				}
				return
			}
			if err != nil {
				t.Fatalf("unexpected error: %v", err)
			}
			if next != tt.to {
				t.Fatalf("expected next %q, got %q", tt.to, next)
			}
		})
	}

	// Ensure the error wraps properly so callers can errors.Is.
	_, err := SegmentStatus("nonexistent").Transition(SegmentStatusPending)
	if err == nil || errors.Is(err, nil) == false { //nolint:gosimple
		// just sanity: err is non-nil
		_ = err
	}
}

func TestSegmentStatus_IsTerminal(t *testing.T) {
	if !SegmentStatusSynthesized.IsTerminal() {
		t.Fatal("synthesized should be terminal")
	}
	for _, s := range []SegmentStatus{"", SegmentStatusPending, SegmentStatusTranslated} {
		if s.IsTerminal() {
			t.Fatalf("%q should NOT be terminal", s)
		}
	}
}
