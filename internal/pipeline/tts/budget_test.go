package tts

import (
	"math"
	"testing"
)

func TestEffectiveDriftThreshold(t *testing.T) {
	tests := []struct {
		name           string
		relThreshold   float64
		absMaxDriftSec float64
		minRelFloor    float64
		targetSec      float64
		want           float64
	}{
		{
			name:           "absolute cap stricter than relative",
			relThreshold:   0.06,
			absMaxDriftSec: 0.5,
			minRelFloor:    0.03,
			targetSec:      20.0, // 0.5/20 = 0.025 < 0.06 → use 0.025; floor 0.03 → 0.03
			want:           0.03,
		},
		{
			name:           "relative tighter than absolute cap",
			relThreshold:   0.06,
			absMaxDriftSec: 5.0,
			minRelFloor:    0.03,
			targetSec:      10.0, // 5/10 = 0.5 > 0.06 → use 0.06
			want:           0.06,
		},
		{
			name:           "no absolute cap",
			relThreshold:   0.06,
			absMaxDriftSec: 0,
			minRelFloor:    0.03,
			targetSec:      10.0,
			want:           0.06,
		},
		{
			name:           "no floor",
			relThreshold:   0.06,
			absMaxDriftSec: 0.5,
			minRelFloor:    0,
			targetSec:      100.0, // 0.5/100 = 0.005, no floor
			want:           0.005,
		},
		{
			name:           "target zero falls back to relative",
			relThreshold:   0.06,
			absMaxDriftSec: 0.5,
			minRelFloor:    0.03,
			targetSec:      0,
			want:           0.06,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := EffectiveDriftThreshold(tt.relThreshold, tt.absMaxDriftSec, tt.minRelFloor, tt.targetSec)
			if math.Abs(got-tt.want) > 1e-9 {
				t.Fatalf("expected %v, got %v", tt.want, got)
			}
		})
	}
}

func TestEffectiveBorrowDriftPct(t *testing.T) {
	tests := []struct {
		name           string
		maxBorrowPct   float64
		absMaxDriftSec float64
		targetMs       int64
		want           float64
	}{
		{
			name:           "abs cap tighter than configured pct",
			maxBorrowPct:   0.12,
			absMaxDriftSec: 0.8,
			targetMs:       20_000, // 0.8/20 = 0.04
			want:           0.04,
		},
		{
			name:           "configured pct tighter than abs cap",
			maxBorrowPct:   0.12,
			absMaxDriftSec: 5.0,
			targetMs:       2_000, // 5/2 = 2.5; 0.12 wins
			want:           0.12,
		},
		{
			name:           "no abs cap",
			maxBorrowPct:   0.12,
			absMaxDriftSec: 0,
			targetMs:       1_000,
			want:           0.12,
		},
		{
			name:           "zero target keeps configured pct",
			maxBorrowPct:   0.12,
			absMaxDriftSec: 0.8,
			targetMs:       0,
			want:           0.12,
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := EffectiveBorrowDriftPct(tt.maxBorrowPct, tt.absMaxDriftSec, tt.targetMs)
			if math.Abs(got-tt.want) > 1e-9 {
				t.Fatalf("expected %v, got %v", tt.want, got)
			}
		})
	}
}

func TestMaxAllowedSec(t *testing.T) {
	if got := MaxAllowedSec(5.0, 2_000); math.Abs(got-7.0) > 1e-9 {
		t.Fatalf("expected 7.0, got %v", got)
	}
	if got := MaxAllowedSec(5.0, -100); math.Abs(got-5.0) > 1e-9 {
		t.Fatalf("expected 5.0 for negative gap, got %v", got)
	}
	if got := MaxAllowedSec(0, 0); math.Abs(got) > 1e-9 {
		t.Fatalf("expected 0, got %v", got)
	}
}

type fakeSeg struct {
	start, end int64
}

func (f fakeSeg) GetStartMs() int64 { return f.start }
func (f fakeSeg) GetEndMs() int64   { return f.end }

func TestGapAfter(t *testing.T) {
	segs := []fakeSeg{
		{start: 0, end: 1_000},
		{start: 1_500, end: 2_000},
		{start: 1_800, end: 3_000}, // overlap with previous (gap should clamp to 0)
	}

	if got := GapAfter(segs, 0); got != 500 {
		t.Fatalf("expected 500, got %d", got)
	}
	if got := GapAfter(segs, 1); got != 0 {
		t.Fatalf("expected 0 for overlapping segments, got %d", got)
	}
	if got := GapAfter(segs, 2); got != DefaultGapAfterMs {
		t.Fatalf("expected DefaultGapAfterMs for last segment, got %d", got)
	}
	if got := GapAfter(segs, 99); got != DefaultGapAfterMs {
		t.Fatalf("expected DefaultGapAfterMs for out-of-range index, got %d", got)
	}
}

func TestDecideOverflow(t *testing.T) {
	tests := []struct {
		name string
		in   OverflowDecisionInput
		want OverflowAction
	}{
		{
			name: "actual within target accept",
			in: OverflowDecisionInput{
				ActualMs:        4_900,
				TargetMs:        5_000,
				GapAfterMs:      2_000,
				MaxBorrowDriftPct: 0.12,
				DriftThreshold:  0.06,
				RetranslationOn: true,
			},
			want: OverflowAccept,
		},
		{
			name: "under-run beyond drift, retranslate",
			in: OverflowDecisionInput{
				ActualMs:        3_000,
				TargetMs:        5_000,
				GapAfterMs:      2_000,
				MaxBorrowDriftPct: 0.12,
				DriftThreshold:  0.06,
				RetranslationOn: true,
			},
			want: OverflowRetranslate,
		},
		{
			name: "under-run beyond drift but retranslation disabled",
			in: OverflowDecisionInput{
				ActualMs:        3_000,
				TargetMs:        5_000,
				GapAfterMs:      2_000,
				MaxBorrowDriftPct: 0.12,
				DriftThreshold:  0.06,
				RetranslationOn: false,
			},
			want: OverflowAccept,
		},
		{
			name: "overflow fits gap and within borrow drift, borrow",
			in: OverflowDecisionInput{
				ActualMs:          5_300,
				TargetMs:          5_000,
				GapAfterMs:        2_000, // > shortGapThreshold(1000); borrowable = 1700
				MaxBorrowDriftPct: 0.12,  // 300/5000 = 0.06 within
				DriftThreshold:    0.06,
				RetranslationOn:   true,
			},
			want: OverflowBorrow,
		},
		{
			name: "overflow exceeds borrow drift, retranslate",
			in: OverflowDecisionInput{
				ActualMs:          6_000, // overflow 1000ms = 20% of 5000ms
				TargetMs:          5_000,
				GapAfterMs:        2_000, // borrowable = 1700ms enough physically
				MaxBorrowDriftPct: 0.12,  // 0.20 > 0.12 → retranslate
				DriftThreshold:    0.06,
				RetranslationOn:   true,
			},
			want: OverflowRetranslate,
		},
		{
			name: "short gap forces retranslate",
			in: OverflowDecisionInput{
				ActualMs:          5_200,
				TargetMs:          5_000,
				GapAfterMs:        500, // <= shortGapThreshold
				MaxBorrowDriftPct: 0.12,
				DriftThreshold:    0.06,
				RetranslationOn:   true,
			},
			want: OverflowRetranslate,
		},
		{
			name: "short gap last attempt, accept",
			in: OverflowDecisionInput{
				ActualMs:          5_200,
				TargetMs:          5_000,
				GapAfterMs:        500,
				MaxBorrowDriftPct: 0.12,
				DriftThreshold:    0.06,
				RetranslationOn:   true,
				IsLastAttempt:     true,
			},
			want: OverflowAccept,
		},
		{
			name: "overflow with retranslation off, accept (was: borrow when fits, accept otherwise)",
			in: OverflowDecisionInput{
				ActualMs:          5_300,
				TargetMs:          5_000,
				GapAfterMs:        2_000,
				MaxBorrowDriftPct: 0.12,
				DriftThreshold:    0.06,
				RetranslationOn:   false,
			},
			want: OverflowBorrow, // overflow fits; borrow regardless of retranslation flag
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := DecideOverflow(tt.in)
			if got != tt.want {
				t.Fatalf("expected %v, got %v", tt.want, got)
			}
		})
	}
}

func TestBlendCharsPerSec(t *testing.T) {
	if got := BlendCharsPerSec(100, 5.0); math.Abs(got-20.0) > 1e-9 {
		t.Fatalf("expected 20, got %v", got)
	}
	if got := BlendCharsPerSec(0, 5); got != 0 {
		t.Fatalf("expected 0 for empty chars, got %v", got)
	}
	if got := BlendCharsPerSec(100, 0); got != 0 {
		t.Fatalf("expected 0 for empty seconds, got %v", got)
	}
}

func TestCountNonWhitespaceRunes(t *testing.T) {
	tests := []struct {
		s    string
		want int
	}{
		{"", 0},
		{"hello", 5},
		{"hello world", 10},
		{"  spaces  in  middle  ", 14},
		{"中文也算字符", 6},
		{"\t\n\r mixed", 5},
	}
	for _, tt := range tests {
		if got := CountNonWhitespaceRunes(tt.s); got != tt.want {
			t.Fatalf("CountNonWhitespaceRunes(%q): expected %d, got %d", tt.s, tt.want, got)
		}
	}
}
