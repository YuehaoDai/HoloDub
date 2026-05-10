// Pure-function tests for the OPT-403/404 download helpers. These run
// unconditionally (no cgo / sqlite needed) so they're exercised on every
// Windows dev machine + every CI build.
package http

import "testing"

func TestDownloadFilenameHelpers(t *testing.T) {
	cases := []struct {
		name     string
		got      string
		wantPart string
	}{
		{"video", downloadFilenameForEpisode(42, "episodes/42/output/vp0/final.mp4"), "episode-42-final.mp4"},
		{"audio", downloadFilenameForEpisode(7, "episodes/7/output/vp0/final.wav"), "episode-7-final.wav"},
		{"emptyExt", downloadFilenameForEpisode(11, "episodes/11/output/vp0/final"), "episode-11-final.mp4"},
		{"job-mp4", downloadFilenameForJob(99, 3, "episodes/1/chapters/vp0/ch03.mp4"), "job-99-ch03.mp4"},
		{"job-noord", downloadFilenameForJob(99, 0, "x.mp4"), "job-99-ch01.mp4"},
		{"job-doubleDigit", downloadFilenameForJob(123, 17, "x.mp4"), "job-123-ch17.mp4"},
		{"contentTypeMp4", contentTypeForPath("/dir/v.mp4"), "video/mp4"},
		{"contentTypeWav", contentTypeForPath("a/b/c.wav"), "audio/wav"},
		{"contentTypeMkv", contentTypeForPath("x.mkv"), "video/x-matroska"},
		{"contentTypeJSON", contentTypeForPath("x.json"), "application/json; charset=utf-8"},
		{"contentTypeUnknown", contentTypeForPath("/no.ext"), "application/octet-stream"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			if tc.got != tc.wantPart {
				t.Errorf("got %q, want %q", tc.got, tc.wantPart)
			}
		})
	}
}

// uintToA / twoDigit are used outside the helpers in the cgo-tagged test
// file too; sanity-check them here so a refactor that breaks the
// formatting pads/digit logic surfaces fast.
func TestUintToA(t *testing.T) {
	cases := map[uint]string{0: "0", 1: "1", 9: "9", 10: "10", 99: "99", 100: "100", 12345: "12345"}
	for in, want := range cases {
		if got := uintToA(in); got != want {
			t.Errorf("uintToA(%d) = %q, want %q", in, got, want)
		}
	}
}

func TestTwoDigit(t *testing.T) {
	cases := map[int]string{0: "00", 1: "01", 9: "09", 10: "10", 99: "99", 100: "100", -3: "03"}
	for in, want := range cases {
		if got := twoDigit(in); got != want {
			t.Errorf("twoDigit(%d) = %q, want %q", in, got, want)
		}
	}
}
