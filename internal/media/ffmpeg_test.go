package media

import "testing"

func TestIsVideoFile(t *testing.T) {
	if !IsVideoFile("jobs/1/input.mp4") {
		t.Fatalf("expected mp4 file to be detected as video")
	}
	if IsVideoFile("jobs/1/input.wav") {
		t.Fatalf("expected wav file not to be detected as video")
	}
}
