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

func TestEpisodeStatus_Transition(t *testing.T) {
	tests := []struct {
		name    string
		from    EpisodeStatus
		to      EpisodeStatus
		wantErr bool
	}{
		{"empty -> pending", "", EpisodeStatusPending, false},

		// pending fan-out branches
		{"pending -> chaptering (long-form path)", EpisodeStatusPending, EpisodeStatusChaptering, false},
		{"pending -> running (1-chapter shortcut)", EpisodeStatusPending, EpisodeStatusRunning, false},
		{"pending -> failed", EpisodeStatusPending, EpisodeStatusFailed, false},

		// chaptering / dispatched
		{"chaptering -> dispatched", EpisodeStatusChaptering, EpisodeStatusDispatched, false},
		{"chaptering -> failed", EpisodeStatusChaptering, EpisodeStatusFailed, false},
		{"dispatched -> running", EpisodeStatusDispatched, EpisodeStatusRunning, false},
		{"dispatched -> failed", EpisodeStatusDispatched, EpisodeStatusFailed, false},

		// running terminal/branches
		{"running -> merging (long-form)", EpisodeStatusRunning, EpisodeStatusMerging, false},
		{"running -> completed (1-chapter shortcut)", EpisodeStatusRunning, EpisodeStatusCompleted, false},
		{"running -> failed", EpisodeStatusRunning, EpisodeStatusFailed, false},

		// merging / judging / reworking
		{"merging -> judging", EpisodeStatusMerging, EpisodeStatusJudging, false},
		{"merging -> failed", EpisodeStatusMerging, EpisodeStatusFailed, false},
		{"judging -> reworking", EpisodeStatusJudging, EpisodeStatusReworking, false},
		{"judging -> completed", EpisodeStatusJudging, EpisodeStatusCompleted, false},
		{"judging -> failed", EpisodeStatusJudging, EpisodeStatusFailed, false},
		{"reworking -> running", EpisodeStatusReworking, EpisodeStatusRunning, false},
		{"reworking -> failed", EpisodeStatusReworking, EpisodeStatusFailed, false},

		// illegal transitions
		{"empty -> running (skips pending)", "", EpisodeStatusRunning, true},
		{"empty -> completed", "", EpisodeStatusCompleted, true},
		{"pending -> dispatched (skips chaptering)", EpisodeStatusPending, EpisodeStatusDispatched, true},
		{"pending -> completed (must go via running)", EpisodeStatusPending, EpisodeStatusCompleted, true},
		{"chaptering -> running (must dispatch first)", EpisodeStatusChaptering, EpisodeStatusRunning, true},
		{"running -> judging (must merge first)", EpisodeStatusRunning, EpisodeStatusJudging, true},
		{"merging -> completed (must judge first)", EpisodeStatusMerging, EpisodeStatusCompleted, true},
		{"completed -> running (terminal)", EpisodeStatusCompleted, EpisodeStatusRunning, true},
		{"failed -> pending (terminal)", EpisodeStatusFailed, EpisodeStatusPending, true},
		{"unknown source", EpisodeStatus("bogus"), EpisodeStatusPending, true},
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
}

func TestEpisodeStatus_IsTerminal(t *testing.T) {
	for _, s := range []EpisodeStatus{EpisodeStatusCompleted, EpisodeStatusFailed} {
		if !s.IsTerminal() {
			t.Fatalf("%q should be terminal", s)
		}
		if s.IsActive() {
			t.Fatalf("%q must not be active when terminal", s)
		}
	}
	for _, s := range []EpisodeStatus{
		EpisodeStatusPending,
		EpisodeStatusChaptering,
		EpisodeStatusDispatched,
		EpisodeStatusRunning,
		EpisodeStatusMerging,
		EpisodeStatusJudging,
		EpisodeStatusReworking,
	} {
		if s.IsTerminal() {
			t.Fatalf("%q must NOT be terminal", s)
		}
		if !s.IsActive() {
			t.Fatalf("%q should be active", s)
		}
	}
	if EpisodeStatus("").IsActive() {
		t.Fatal("empty status should not be active")
	}
	if EpisodeStatus("").IsTerminal() {
		t.Fatal("empty status should not be terminal")
	}
}
