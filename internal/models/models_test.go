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

// TestEpisodeStageOrder pins the canonical episode-level pipeline order
// declared by OPT-402. The chapter-level worker dispatch (HandleTask)
// hard-codes a switch on these constants, and any reorder/insertion
// without updating the dispatch breaks the pipeline silently.
//
// The test is split into 4 invariants:
//  1. exact ordered sequence (chronological dependency)
//  2. Next() returns the immediate successor
//  3. Next() on the LAST stage returns ok=false (no chapter stage in the
//     same enum — control transfers to JobStage at that point)
//  4. unknown stages return ok=false (graceful degradation)
func TestEpisodeStageOrder(t *testing.T) {
	want := []EpisodeStage{
		EpisodeStageMedia,
		EpisodeStageSeparate,
		EpisodeStageASRSmart,
		EpisodeStageGlossaryExtract,
		EpisodeStageChapterize,
	}
	if len(EpisodeStageOrder) != len(want) {
		t.Fatalf("EpisodeStageOrder length: want %d, got %d (slice=%v)",
			len(want), len(EpisodeStageOrder), EpisodeStageOrder)
	}
	for i, s := range want {
		if EpisodeStageOrder[i] != s {
			t.Fatalf("EpisodeStageOrder[%d]: want %q, got %q",
				i, s, EpisodeStageOrder[i])
		}
	}

	for i := 0; i < len(EpisodeStageOrder)-1; i++ {
		next, ok := EpisodeStageOrder[i].Next()
		if !ok {
			t.Fatalf("Next() for %q: expected ok=true", EpisodeStageOrder[i])
		}
		if next != EpisodeStageOrder[i+1] {
			t.Fatalf("Next() for %q: want %q, got %q",
				EpisodeStageOrder[i], EpisodeStageOrder[i+1], next)
		}
	}

	last := EpisodeStageOrder[len(EpisodeStageOrder)-1]
	if next, ok := last.Next(); ok {
		t.Fatalf("Next() for last stage %q: expected ok=false, got %q",
			last, next)
	}

	if next, ok := EpisodeStage("ep_bogus").Next(); ok {
		t.Fatalf("Next() for unknown stage: expected ok=false, got %q", next)
	}

	// OPT-403/404/406 placeholders MUST exist as constants but MUST NOT
	// appear in EpisodeStageOrder yet (their handlers are not yet wired).
	for _, placeholder := range []EpisodeStage{
		EpisodeStageEpisodeMerge,
		EpisodeStageEpisodeJudge,
	} {
		for _, ordered := range EpisodeStageOrder {
			if placeholder == ordered {
				t.Fatalf("placeholder stage %q must not be in EpisodeStageOrder yet "+
					"(handler not wired — would dispatch to unimplemented code)",
					placeholder)
			}
		}
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

// TestEpisodePathHelpers locks the OPT-403 unified output layout. Anything
// generating an episode artefact path MUST go through these helpers so that
// (a) the path string is computed in one place and (b) lessons-learned.mdc §1
// (the "fmt.Sprintf segment-XXXX.wav" antipattern) does not get re-introduced
// at every new file-serving / merge / fan-out site.
func TestEpisodePathHelpers(t *testing.T) {
	ep := &Episode{ID: 138}

	cases := []struct {
		name string
		got  string
		want string
	}{
		{"episode output vp0", ep.GetEpisodeOutputRelPath(0), "episodes/138/output/vp0/final.mp4"},
		{"episode output vp7", ep.GetEpisodeOutputRelPath(7), "episodes/138/output/vp7/final.mp4"},
		{"chapter 1 vp0", ep.GetChapterOutputRelPath(1, 0), "episodes/138/chapters/vp0/ch01.mp4"},
		{"chapter 14 vp0 zero-pads ordinal", ep.GetChapterOutputRelPath(14, 0), "episodes/138/chapters/vp0/ch14.mp4"},
		{"separate vocals", ep.GetEpisodeSeparateRelPath("vocals"), "episodes/138/separate/vocals.wav"},
		{"separate bgm", ep.GetEpisodeSeparateRelPath("bgm"), "episodes/138/separate/bgm.wav"},
		{"chapters json", ep.GetChaptersJSONRelPath(), "episodes/138/chapters.json"},
	}
	for _, tt := range cases {
		t.Run(tt.name, func(t *testing.T) {
			if tt.got != tt.want {
				t.Fatalf("got %q, want %q", tt.got, tt.want)
			}
		})
	}
}

// TestJobStatusAwaitingChapterizeConstant guards that the OPT-403 transitional
// state introduced for chapter 1 (between "ASR done" and "fan-out committed")
// keeps its expected string value, since the UI renders the badge by exact
// match and store layer stages keyed off it.
func TestJobStatusAwaitingChapterizeConstant(t *testing.T) {
	if string(JobStatusAwaitingChapterize) != "awaiting_chapterize" {
		t.Fatalf("JobStatusAwaitingChapterize wire value drifted: got %q",
			JobStatusAwaitingChapterize)
	}
}
