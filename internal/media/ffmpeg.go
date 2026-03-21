package media

import (
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"

	"holodub/internal/storage"
)

type AudioOverlay struct {
	RelPath       string
	DelayMs       int64
	DurationMs    int64
	// MaxDurationMs is the hard playback ceiling for this overlay — the gap
	// to the next segment start.  Audio beyond this point would overlap the
	// next sentence.  Zero means unconstrained (use DurationMs as ceiling).
	MaxDurationMs int64
}

// maxOverlaysPerPass caps the number of TTS segments in a single ffmpeg
// filter_complex call.  Beyond ~50 inputs, ffmpeg's graph parser becomes
// extremely slow (O(N²)) and can hang for hours on large videos.
// Use 30 to stay well below the threshold; 50 was observed to hang on 626-segment jobs.
const maxOverlaysPerPass = 30

// RenderDubTrack builds the final mixed audio track.  For large videos
// (many segments) it processes overlays in batches of maxOverlaysPerPass,
// merges the batch tracks, then applies BGM in a final pass.
func RenderDubTrack(dataRoot, ffmpegBin string, outputRelPath string, durationMs int64, bgmRelPath string, overlays []AudioOverlay) error {
	outputPath := storage.ResolveDataPath(dataRoot, outputRelPath)
	if err := storage.EnsureParentDir(outputPath); err != nil {
		return err
	}

	if len(overlays) <= maxOverlaysPerPass {
		return renderDubTrackDirect(dataRoot, ffmpegBin, outputPath, durationMs, bgmRelPath, overlays)
	}

	// --- Chunked path for large segment counts ---
	// Step 1: build one full-duration voice track per chunk (no BGM yet).
	tmpDir := filepath.Dir(outputPath)
	var chunkPaths []string
	for i := 0; i < len(overlays); i += maxOverlaysPerPass {
		end := i + maxOverlaysPerPass
		if end > len(overlays) {
			end = len(overlays)
		}
		chunkPath := filepath.Join(tmpDir, fmt.Sprintf("_chunk_%d.wav", i))
		if err := buildVoiceChunk(dataRoot, ffmpegBin, chunkPath, durationMs, overlays[i:end]); err != nil {
			cleanupFiles(chunkPaths)
			return fmt.Errorf("voice chunk %d-%d: %w", i, end, err)
		}
		chunkPaths = append(chunkPaths, chunkPath)
	}
	defer cleanupFiles(chunkPaths)

	// Step 2: merge chunk tracks into one voice track, then apply BGM.
	voicePath := filepath.Join(tmpDir, "_voice_merged.wav")
	defer os.Remove(voicePath)
	if err := mergeVoiceChunks(ffmpegBin, voicePath, durationMs, chunkPaths); err != nil {
		return fmt.Errorf("merge voice chunks: %w", err)
	}

	return applyBGM(dataRoot, ffmpegBin, voicePath, bgmRelPath, outputPath, durationMs)
}

// renderDubTrackDirect is the original single-pass approach, used when the
// segment count is small enough for one filter_complex call.
func renderDubTrackDirect(dataRoot, ffmpegBin, outputPath string, durationMs int64, bgmRelPath string, overlays []AudioOverlay) error {
	args := []string{
		"-y",
		"-f", "lavfi",
		"-t", formatSeconds(durationMs),
		"-i", "anullsrc=r=24000:cl=mono",
	}

	filterParts := []string{}
	voiceLabels := []string{}

	if bgmRelPath != "" {
		args = append(args, "-i", storage.ResolveDataPath(dataRoot, bgmRelPath))
		filterParts = append(filterParts, "[1:a]aresample=24000,volume=0.75,alimiter=limit=0.95[bgm]")
	}

	baseIndex := 1
	if bgmRelPath != "" {
		baseIndex = 2
	}
	for idx, overlay := range overlays {
		inputIndex := baseIndex + idx
		args = append(args, "-i", storage.ResolveDataPath(dataRoot, overlay.RelPath))
		label := fmt.Sprintf("seg%d", idx)
		filterParts = append(filterParts, fmt.Sprintf("[%d:a]%sadelay=%d|%d,alimiter=limit=0.95[%s]", inputIndex, clipAndFadeFilter(overlay.DurationMs, overlay.MaxDurationMs), overlay.DelayMs, overlay.DelayMs, label))
		voiceLabels = append(voiceLabels, fmt.Sprintf("[%s]", label))
	}

	var voiceOut string
	voiceOut, filterParts = buildVoiceMix(filterParts, voiceLabels)

	if bgmRelPath != "" {
		filterParts = append(filterParts, fmt.Sprintf("[%s]asplit=2[sc][mixin]", voiceOut))
		filterParts = append(filterParts, "[bgm][sc]sidechaincompress=threshold=0.015:ratio=8:attack=20:release=250[duckedbgm]")
		filterParts = append(filterParts, "[0:a][duckedbgm][mixin]amix=inputs=3:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95,aresample=24000[mix]")
	} else {
		filterParts = append(filterParts, fmt.Sprintf("[0:a][%s]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95,aresample=24000[mix]", voiceOut))
	}
	args = append(args,
		"-filter_complex", strings.Join(filterParts, ";"),
		"-map", "[mix]",
		"-ar", "24000",
		"-ac", "1",
		outputPath,
	)
	return runCmd(ffmpegBin, args...)
}

// buildVoiceChunk renders a small batch of overlays into a full-duration
// voice-only WAV (no BGM).  The rest of the duration is silence.
func buildVoiceChunk(dataRoot, ffmpegBin, chunkPath string, durationMs int64, overlays []AudioOverlay) error {
	args := []string{
		"-y",
		"-f", "lavfi",
		"-t", formatSeconds(durationMs),
		"-i", "anullsrc=r=24000:cl=mono",
	}
	filterParts := []string{}
	voiceLabels := []string{}

	for idx, overlay := range overlays {
		inputIndex := 1 + idx
		args = append(args, "-i", storage.ResolveDataPath(dataRoot, overlay.RelPath))
		label := fmt.Sprintf("s%d", idx)
		filterParts = append(filterParts, fmt.Sprintf("[%d:a]%sadelay=%d|%d,alimiter=limit=0.95[%s]", inputIndex, clipAndFadeFilter(overlay.DurationMs, overlay.MaxDurationMs), overlay.DelayMs, overlay.DelayMs, label))
		voiceLabels = append(voiceLabels, fmt.Sprintf("[%s]", label))
	}

	var voiceOut string
	voiceOut, filterParts = buildVoiceMix(filterParts, voiceLabels)
	filterParts = append(filterParts, fmt.Sprintf("[0:a][%s]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95,aresample=24000[mix]", voiceOut))

	args = append(args,
		"-filter_complex", strings.Join(filterParts, ";"),
		"-map", "[mix]",
		"-ar", "24000",
		"-ac", "1",
		chunkPath,
	)
	return runCmd(ffmpegBin, args...)
}

// mergeVoiceChunks mixes all per-chunk voice tracks into a single voice track.
// At any timestamp only one chunk has non-silent audio, so amix with
// normalize=0 is equivalent to a lossless sum.
func mergeVoiceChunks(ffmpegBin, voicePath string, durationMs int64, chunkPaths []string) error {
	if len(chunkPaths) == 1 {
		return copyFile(chunkPaths[0], voicePath)
	}
	args := []string{"-y"}
	labels := []string{}
	for i, p := range chunkPaths {
		args = append(args, "-i", p)
		labels = append(labels, fmt.Sprintf("[%d:a]", i))
	}
	filter := fmt.Sprintf("%samix=inputs=%d:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95[mix]",
		strings.Join(labels, ""), len(chunkPaths))
	args = append(args, "-filter_complex", filter, "-map", "[mix]", "-ar", "24000", "-ac", "1", voicePath)
	return runCmd(ffmpegBin, args...)
}

// applyBGM mixes a pre-built voice WAV with optional BGM using sidechain
// compression, and writes the final dub track.
func applyBGM(dataRoot, ffmpegBin, voicePath, bgmRelPath, outputPath string, durationMs int64) error {
	if bgmRelPath == "" {
		return copyFile(voicePath, outputPath)
	}
	bgmPath := storage.ResolveDataPath(dataRoot, bgmRelPath)
	args := []string{
		"-y",
		"-i", voicePath,
		"-i", bgmPath,
		"-filter_complex",
		"[1:a]aresample=24000,volume=0.75,alimiter=limit=0.95[bgm];" +
			"[0:a]asplit=2[sc][mixin];" +
			"[bgm][sc]sidechaincompress=threshold=0.015:ratio=8:attack=20:release=250[duckedbgm];" +
			"[duckedbgm][mixin]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,alimiter=limit=0.95[mix]",
		"-map", "[mix]",
		"-ar", "24000",
		"-ac", "1",
		outputPath,
	}
	return runCmd(ffmpegBin, args...)
}

// buildVoiceMix appends the amix/volume filter to filterParts and returns
// the output label name.
func buildVoiceMix(filterParts []string, voiceLabels []string) (string, []string) {
	voiceOut := "vout"
	if len(voiceLabels) == 1 {
		filterParts = append(filterParts, fmt.Sprintf("%svolume=1.0[%s]", voiceLabels[0], voiceOut))
	} else {
		filterParts = append(filterParts, fmt.Sprintf("%samix=inputs=%d:duration=longest:dropout_transition=0:normalize=0,volume=1.0[%s]",
			strings.Join(voiceLabels, ""), len(voiceLabels), voiceOut))
	}
	return voiceOut, filterParts
}

// runCmd runs an external command and wraps any error with its combined stdout+stderr output.
func runCmd(name string, args ...string) error {
	out, err := exec.Command(name, args...).CombinedOutput()
	if err != nil {
		if len(out) > 0 {
			return fmt.Errorf("%w\n%s", err, string(out))
		}
		return err
	}
	return nil
}

func copyFile(src, dst string) error {
	srcF, err := os.Open(src)
	if err != nil {
		return err
	}
	defer srcF.Close()
	dstF, err := os.Create(dst)
	if err != nil {
		return err
	}
	defer dstF.Close()
	_, err = io.Copy(dstF, srcF)
	return err
}

func cleanupFiles(paths []string) {
	for _, p := range paths {
		os.Remove(p)
	}
}

func MuxVideo(dataRoot, ffmpegBin, inputRelPath, audioRelPath, outputRelPath string) error {
	outputPath := storage.ResolveDataPath(dataRoot, outputRelPath)
	if err := storage.EnsureParentDir(outputPath); err != nil {
		return err
	}

	args := []string{
		"-y",
		"-i", storage.ResolveDataPath(dataRoot, inputRelPath),
		"-i", storage.ResolveDataPath(dataRoot, audioRelPath),
		"-map", "0:v:0",
		"-map", "1:a:0",
		"-c:v", "copy",
		"-c:a", "aac",
		"-shortest",
		outputPath,
	}
	return runCmd(ffmpegBin, args...)
}

func ProbeDurationMs(dataRoot, ffprobeBin, relPath string) (int64, error) {
	args := []string{
		"-v", "error",
		"-show_entries", "format=duration",
		"-of", "default=noprint_wrappers=1:nokey=1",
		storage.ResolveDataPath(dataRoot, relPath),
	}
	output, err := exec.Command(ffprobeBin, args...).Output()
	if err != nil {
		return 0, err
	}
	seconds, err := strconv.ParseFloat(strings.TrimSpace(string(output)), 64)
	if err != nil {
		return 0, err
	}
	return int64(seconds * 1000), nil
}

func IsVideoFile(relPath string) bool {
	switch strings.ToLower(filepath.Ext(relPath)) {
	case ".mp4", ".mov", ".mkv", ".webm":
		return true
	default:
		return false
	}
}

func formatSeconds(durationMs int64) string {
	return strconv.FormatFloat(float64(durationMs)/1000.0, 'f', 3, 64)
}

// TrimAudioSegment extracts a time range from an audio file.
// startMs and endMs are in milliseconds; output is written to outPath.
func TrimAudioSegment(ffmpegBin, inputPath, outPath string, startMs, endMs int64) error {
	if err := storage.EnsureParentDir(outPath); err != nil {
		return err
	}
	durationMs := endMs - startMs
	if durationMs <= 0 {
		return fmt.Errorf("invalid segment: start %d >= end %d", startMs, endMs)
	}
	startSec := float64(startMs) / 1000.0
	durSec := float64(durationMs) / 1000.0
	args := []string{
		"-y",
		"-ss", strconv.FormatFloat(startSec, 'f', 3, 64),
		"-i", inputPath,
		"-t", strconv.FormatFloat(durSec, 'f', 3, 64),
		"-acodec", "pcm_s16le",
		"-ar", "24000",
		"-ac", "1",
		outPath,
	}
	return runCmd(ffmpegBin, args...)
}

func overlayFadeFilter(durationMs int64) string {
	if durationMs <= 80 {
		return "aresample=24000,"
	}
	fadeDurationSec := 0.03
	fadeOutStartSec := (float64(durationMs) / 1000.0) - fadeDurationSec
	return fmt.Sprintf("aresample=24000,afade=t=in:st=0:d=%.2f,afade=t=out:st=%.2f:d=%.2f,", fadeDurationSec, fadeOutStartSec, fadeDurationSec)
}

// clipAndFadeFilter returns a filter string that hard-clips the audio to
// maxDurationMs and applies a short fade-in/out.  This prevents a TTS segment
// that ran longer than its time slot from spilling audio into the next segment.
func clipAndFadeFilter(actualDurationMs, maxDurationMs int64) string {
	clip := actualDurationMs
	if maxDurationMs > 0 && maxDurationMs < clip {
		clip = maxDurationMs
	}
	if clip <= 80 {
		return fmt.Sprintf("aresample=24000,atrim=duration=%.3f,", float64(clip)/1000.0)
	}
	fadeDurationSec := 0.03
	fadeOutStartSec := float64(clip)/1000.0 - fadeDurationSec
	if fadeOutStartSec < 0 {
		fadeOutStartSec = 0
	}
	return fmt.Sprintf("aresample=24000,atrim=duration=%.3f,afade=t=in:st=0:d=%.2f,afade=t=out:st=%.2f:d=%.2f,",
		float64(clip)/1000.0, fadeDurationSec, fadeOutStartSec, fadeDurationSec)
}
