package media

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
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

// runCmd runs an external command and wraps any error with its combined
// stdout+stderr output. It is the legacy variant kept for callers that have
// not been threaded with a context yet; new code should prefer runCmdCtx.
func runCmd(name string, args ...string) error {
	return runCmdCtx(context.Background(), name, args...)
}

// runCmdCtx is the cancellable variant of runCmd. When ctx is cancelled the
// underlying ffmpeg/ffprobe process is sent SIGKILL and the call returns
// ctx.Err() (so callers can distinguish cancellation from real failures).
func runCmdCtx(ctx context.Context, name string, args ...string) error {
	out, err := exec.CommandContext(ctx, name, args...).CombinedOutput()
	if err != nil {
		// Surface context cancellation transparently: when ctx fires while
		// ffmpeg is running, exec.CommandContext returns "signal: killed"
		// which is not actionable. Return ctx.Err() instead so the worker
		// can shut down cleanly without spurious "task failed" alerts.
		if ctxErr := ctx.Err(); ctxErr != nil {
			return ctxErr
		}
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

// ───────────────────────────────────────────────────────────────────────────
// OPT-403 helpers — chapter slicing, loudness normalisation, concatenation
// ───────────────────────────────────────────────────────────────────────────

// SliceVideoAtRange extracts the [startMs, endMs) range from inputRelPath into
// outRelPath using stream-copy (-c copy). Near-instant; keyframe alignment may
// shift the actual cut by ±1 GOP (~100 ms) but OPT-403 places chapter
// boundaries inside silence gaps so the visual cut sits in dead air anyway.
//
// Used by stage_chapterize when slicing the source video into chapter videos.
// The source vocals/BGM tracks are sliced separately (TrimAudioSegment) since
// they are sample-accurate WAV.
func SliceVideoAtRange(dataRoot, ffmpegBin, inputRelPath, outRelPath string, startMs, endMs int64) error {
	if startMs < 0 || endMs <= startMs {
		return fmt.Errorf("SliceVideoAtRange: invalid range [%d, %d)", startMs, endMs)
	}
	inPath := storage.ResolveDataPath(dataRoot, inputRelPath)
	outPath := storage.ResolveDataPath(dataRoot, outRelPath)
	if err := storage.EnsureParentDir(outPath); err != nil {
		return err
	}
	durationMs := endMs - startMs
	args := []string{
		"-y",
		"-ss", formatSeconds(startMs),
		"-i", inPath,
		"-t", formatSeconds(durationMs),
		"-c", "copy",
		"-avoid_negative_ts", "make_zero",
		outPath,
	}
	return runCmd(ffmpegBin, args...)
}

// LoudnormStats are the EBU R128 measurements ffmpeg's loudnorm filter prints
// to stderr at the end of pass 1 (and pass 2 in summary mode). Persisted to
// Episode.LoudnormStats so the UI / chapter judge can surface deviations.
//
// Field semantics (LUFS = Loudness Units Full Scale, dBTP = decibels True Peak):
//   - InputI / OutputI  integrated loudness across the file
//   - InputTP / OutputTP  highest true peak detected
//   - InputLRA / OutputLRA  loudness range (perceptual dynamic range)
//   - InputThresh / OutputThresh  relative gating threshold
//   - NormType  "linear" (single coefficient) or "dynamic" (compressor)
//   - TargetOffset  gain offset applied during pass 2 (dB)
type LoudnormStats struct {
	InputI       float64 `json:"input_i"`
	InputTP      float64 `json:"input_tp"`
	InputLRA     float64 `json:"input_lra"`
	InputThresh  float64 `json:"input_thresh"`
	OutputI      float64 `json:"output_i"`
	OutputTP     float64 `json:"output_tp"`
	OutputLRA    float64 `json:"output_lra"`
	OutputThresh float64 `json:"output_thresh"`
	NormType     string  `json:"normalization_type"`
	TargetOffset float64 `json:"target_offset"`
}

// LoudnormTwoPass runs EBU R128 loudness normalisation on inputPath in two
// passes:
//
//   - Pass 1: measure (loudnorm=...:print_format=json), parse the JSON block
//     printed to stderr.
//   - Pass 2: apply linear normalisation seeded with the pass-1 measurements,
//     write the normalised audio to outPath.
//
// Returns the pass-1 measurements regardless of pass-2 success so that on
// pass-2 failure the caller can still log "what we measured".
//
// Defaults follow EBU R128 broadcast: I=-23 LUFS / TP=-1.0 dBTP / LRA=7 LU.
// Pass cfg.Loudnorm* knobs from caller — values < 0 are kept as-is, but a
// targetTP of 0 dBTP is silently snapped to -1.0 to avoid hard clipping
// the AAC encoder.
func LoudnormTwoPass(
	ctx context.Context,
	ffmpegBin, inputPath, outPath string,
	targetI, targetTP, targetLRA float64,
) (LoudnormStats, error) {
	if targetTP == 0 {
		targetTP = -1.0
	}
	pass1Args := []string{
		"-y", "-i", inputPath,
		"-af", fmt.Sprintf("loudnorm=I=%.2f:TP=%.2f:LRA=%.2f:print_format=json",
			targetI, targetTP, targetLRA),
		"-f", "null", "-",
	}
	cmd := exec.CommandContext(ctx, ffmpegBin, pass1Args...)
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		if ctxErr := ctx.Err(); ctxErr != nil {
			return LoudnormStats{}, ctxErr
		}
		return LoudnormStats{}, fmt.Errorf("loudnorm pass 1: %w\nstderr: %s",
			err, truncateForLog(stderr.String(), 2000))
	}
	stats, err := parseLoudnormJSON(stderr.String())
	if err != nil {
		return LoudnormStats{}, fmt.Errorf("loudnorm pass 1 parse: %w\nstderr tail: %s",
			err, truncateForLog(stderr.String(), 800))
	}

	if err := storage.EnsureParentDir(outPath); err != nil {
		return stats, err
	}
	pass2Filter := fmt.Sprintf(
		"loudnorm=I=%.2f:TP=%.2f:LRA=%.2f:"+
			"measured_I=%.6f:measured_TP=%.6f:measured_LRA=%.6f:measured_thresh=%.6f:"+
			"offset=%.6f:linear=true:print_format=summary",
		targetI, targetTP, targetLRA,
		stats.InputI, stats.InputTP, stats.InputLRA, stats.InputThresh, stats.TargetOffset,
	)
	pass2Args := []string{
		"-y", "-i", inputPath,
		"-af", pass2Filter,
		"-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
		outPath,
	}
	if err := runCmdCtx(ctx, ffmpegBin, pass2Args...); err != nil {
		return stats, fmt.Errorf("loudnorm pass 2: %w", err)
	}
	return stats, nil
}

// ConcatChapterVideos concatenates ordered chapter videos into outPath using
// ffmpeg's concat demuxer (no re-encoding). All inputs MUST share codec,
// sample rate and timebase — chapter videos produced by stage_merge always
// do because they come out of the same encoder settings, so this fast path
// is safe by construction.
//
// Single-input case is a plain file copy. Empty input is an error so the
// caller never silently produces an empty mp4.
func ConcatChapterVideos(ctx context.Context, ffmpegBin string, inputPaths []string, outPath string) error {
	if len(inputPaths) == 0 {
		return errors.New("ConcatChapterVideos: no input paths")
	}
	if err := storage.EnsureParentDir(outPath); err != nil {
		return err
	}
	if len(inputPaths) == 1 {
		return copyFile(inputPaths[0], outPath)
	}

	// Write a temp concat list inside outPath's directory so it inherits the
	// same filesystem permissions and gets cleaned up next to the result.
	listFile, err := os.CreateTemp(filepath.Dir(outPath), "concat_*.txt")
	if err != nil {
		return fmt.Errorf("ConcatChapterVideos: create concat list: %w", err)
	}
	listPath := listFile.Name()
	defer os.Remove(listPath)

	for _, p := range inputPaths {
		absP, err := filepath.Abs(p)
		if err != nil {
			listFile.Close()
			return fmt.Errorf("ConcatChapterVideos: resolve abs path %q: %w", p, err)
		}
		// ffmpeg concat format: file 'PATH' — escape single quotes by closing
		// the quoted span, inserting an escaped quote, and reopening.
		escaped := strings.ReplaceAll(absP, "'", "'\\''")
		if _, err := fmt.Fprintf(listFile, "file '%s'\n", escaped); err != nil {
			listFile.Close()
			return fmt.Errorf("ConcatChapterVideos: write concat list: %w", err)
		}
	}
	if err := listFile.Close(); err != nil {
		return fmt.Errorf("ConcatChapterVideos: close concat list: %w", err)
	}

	args := []string{
		"-y",
		"-f", "concat",
		"-safe", "0",
		"-i", listPath,
		"-c", "copy",
		outPath,
	}
	return runCmdCtx(ctx, ffmpegBin, args...)
}

// parseLoudnormJSON extracts the trailing JSON block ffmpeg's loudnorm filter
// prints to stderr (it appears AFTER the regular ffmpeg log lines, so we scan
// from the end for the last balanced { ... } block). Returns LoudnormStats
// with the eight measurements parsed; any unparseable numeric is returned as 0
// (and we log the raw string in the caller's wrapping error).
//
// Exported in test-friendly form: parseLoudnormJSON(string) is exercised
// directly by ffmpeg_test.go without spawning ffmpeg.
func parseLoudnormJSON(stderr string) (LoudnormStats, error) {
	end := strings.LastIndex(stderr, "}")
	if end < 0 {
		return LoudnormStats{}, errors.New("loudnorm: no JSON block found in stderr")
	}
	start := strings.LastIndex(stderr[:end], "{")
	if start < 0 {
		return LoudnormStats{}, errors.New("loudnorm: unbalanced braces in stderr")
	}
	var raw struct {
		InputI       string `json:"input_i"`
		InputTP      string `json:"input_tp"`
		InputLRA     string `json:"input_lra"`
		InputThresh  string `json:"input_thresh"`
		OutputI      string `json:"output_i"`
		OutputTP     string `json:"output_tp"`
		OutputLRA    string `json:"output_lra"`
		OutputThresh string `json:"output_thresh"`
		NormType     string `json:"normalization_type"`
		TargetOffset string `json:"target_offset"`
	}
	if err := json.Unmarshal([]byte(stderr[start:end+1]), &raw); err != nil {
		return LoudnormStats{}, fmt.Errorf("loudnorm: unmarshal JSON: %w", err)
	}
	parse := func(s string) float64 {
		v, err := strconv.ParseFloat(strings.TrimSpace(s), 64)
		if err != nil {
			return 0
		}
		return v
	}
	return LoudnormStats{
		InputI:       parse(raw.InputI),
		InputTP:      parse(raw.InputTP),
		InputLRA:     parse(raw.InputLRA),
		InputThresh:  parse(raw.InputThresh),
		OutputI:      parse(raw.OutputI),
		OutputTP:     parse(raw.OutputTP),
		OutputLRA:    parse(raw.OutputLRA),
		OutputThresh: parse(raw.OutputThresh),
		NormType:     raw.NormType,
		TargetOffset: parse(raw.TargetOffset),
	}, nil
}

// truncateForLog clips a string to maxLen runes, replacing the middle with
// "..." when it exceeds the budget. Used to keep ffmpeg stderr readable in
// error wrappers without flooding logs.
func truncateForLog(s string, maxLen int) string {
	if maxLen <= 0 || len(s) <= maxLen {
		return s
	}
	half := maxLen / 2
	return s[:half] + "...[truncated " + strconv.Itoa(len(s)-maxLen) + " bytes]..." + s[len(s)-half:]
}
