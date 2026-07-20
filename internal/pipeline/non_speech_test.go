package pipeline

import (
	"testing"

	"holodub/internal/models"
)

func TestShouldSkipDubbing(t *testing.T) {
	cases := []struct {
		name string
		seg  models.Segment
		want bool
	}{
		{
			name: "empty source",
			seg:  models.Segment{SourceText: "   "},
			want: true,
		},
		{
			name: "split reason non speech",
			seg:  models.Segment{SourceText: "noise", SplitReason: "non_speech:high_zcr"},
			want: true,
		},
		{
			name: "meta guard drop",
			seg: models.Segment{
				SourceText: "fake text",
				Meta: map[string]any{
					asrGuardMetaKey: map[string]any{"drop": true},
				},
			},
			want: true,
		},
		{
			name: "normal speech",
			seg:  models.Segment{SourceText: "Today we discuss cache consistency."},
			want: false,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if got := shouldSkipDubbing(tc.seg); got != tc.want {
				t.Fatalf("shouldSkipDubbing() = %v, want %v", got, tc.want)
			}
		})
	}
}

func TestMarkSegmentSkipped(t *testing.T) {
	seg := models.Segment{
		SourceText:      "chalk squeak",
		TargetText:      "chalk sound",
		TTSAudioRelPath: "jobs/1/tts/segment.wav",
		TTSDurationMs:   1200,
		Status:          models.SegmentStatusTranslated,
	}

	markSegmentSkipped(&seg, "high_zcr")

	if seg.Status != models.SegmentStatusSkipped {
		t.Fatalf("status = %q, want skipped", seg.Status)
	}
	if seg.TargetText != "" || seg.TTSAudioRelPath != "" || seg.TTSDurationMs != 0 {
		t.Fatalf("expected TTS fields cleared, got target=%q path=%q dur=%d",
			seg.TargetText, seg.TTSAudioRelPath, seg.TTSDurationMs)
	}
	raw, ok := seg.Meta[skipMetaKey].(map[string]any)
	if !ok {
		t.Fatalf("skip meta missing: %+v", seg.Meta)
	}
	if raw["reason"] != "high_zcr" {
		t.Fatalf("skip reason = %v, want high_zcr", raw["reason"])
	}
}
