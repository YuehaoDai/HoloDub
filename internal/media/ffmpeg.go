package media

import (
	"fmt"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"

	"holodub/internal/storage"
)

type AudioOverlay struct {
	RelPath    string
	DelayMs    int64
	DurationMs int64
}

func RenderDubTrack(dataRoot, ffmpegBin string, outputRelPath string, durationMs int64, bgmRelPath string, overlays []AudioOverlay) error {
	outputPath := storage.ResolveDataPath(dataRoot, outputRelPath)
	if err := storage.EnsureParentDir(outputPath); err != nil {
		return err
	}

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
		filterParts = append(filterParts, fmt.Sprintf("[%d:a]%sadelay=%d|%d,alimiter=limit=0.95[%s]", inputIndex, overlayFadeFilter(overlay.DurationMs), overlay.DelayMs, overlay.DelayMs, label))
		voiceLabels = append(voiceLabels, fmt.Sprintf("[%s]", label))
	}

	voiceOut := "vout"
	if len(voiceLabels) == 1 {
		filterParts = append(filterParts, fmt.Sprintf("%svolume=1.0[%s]", voiceLabels[0], voiceOut))
	} else {
		filterParts = append(filterParts, fmt.Sprintf("%samix=inputs=%d:duration=longest:dropout_transition=0,volume=1.0[%s]", strings.Join(voiceLabels, ""), len(voiceLabels), voiceOut))
	}
	if bgmRelPath != "" {
		// asplit duplicates voice so it can feed both sidechaincompress (sidechain) and amix; pad labels cannot be reused
		filterParts = append(filterParts, fmt.Sprintf("[%s]asplit=2[sc][mixin]", voiceOut))
		filterParts = append(filterParts, "[bgm][sc]sidechaincompress=threshold=0.015:ratio=8:attack=20:release=250[duckedbgm]")
		filterParts = append(filterParts, "[0:a][duckedbgm][mixin]amix=inputs=3:duration=first:dropout_transition=0,alimiter=limit=0.95,aresample=24000[mix]")
	} else {
		filterParts = append(filterParts, fmt.Sprintf("[0:a][%s]amix=inputs=2:duration=first:dropout_transition=0,alimiter=limit=0.95,aresample=24000[mix]", voiceOut))
	}
	args = append(args,
		"-filter_complex", strings.Join(filterParts, ";"),
		"-map", "[mix]",
		"-ar", "24000",
		"-ac", "1",
		outputPath,
	)

	return exec.Command(ffmpegBin, args...).Run()
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
	return exec.Command(ffmpegBin, args...).Run()
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

func overlayFadeFilter(durationMs int64) string {
	if durationMs <= 80 {
		return "aresample=24000,"
	}
	fadeDurationSec := 0.03
	fadeOutStartSec := (float64(durationMs) / 1000.0) - fadeDurationSec
	return fmt.Sprintf("aresample=24000,afade=t=in:st=0:d=%.2f,afade=t=out:st=%.2f:d=%.2f,", fadeDurationSec, fadeOutStartSec, fadeDurationSec)
}
