// Episode + chapter file-download HTTP handlers (OPT-403/404 unified
// output layout). Three routes are exposed:
//
//   - GET /episodes/:id/download/final
//     Episode-level final.mp4 written by ep_episode_merge to
//     episodes/{ep_id}/output/vp{vp}/final.mp4 (Episode.OutputRelPath).
//
//   - GET /episodes/:id/chapters.json
//     The bilingual manifest written alongside the episode-level final
//     at episodes/{ep_id}/chapters.json (Episode.ChaptersManifestRelPath).
//
//   - GET /jobs/:id/download/final
//     Chapter-level final.mp4 stored at Job.OutputRelPath. Works for
//     both OPT-403 v2 layout (episodes/{ep_id}/chapters/...) and the
//     legacy v1 layout (jobs/{id}/output/...) — handler reads the path
//     from the DB row, never constructs paths from naming conventions.
//     This is the lessons-learned 1 invariant; do not refactor to
//     auto-build the path.
//
// All three handlers honour the existing tenant scoping helpers and
// respond with the same {error, message} envelope as the other file-
// serving endpoints (serveSegmentAudio etc.).
package http

import (
	stdhttp "net/http"
	"os"
	"path/filepath"
	"strings"

	"holodub/internal/storage"

	"github.com/gin-gonic/gin"
)

// serveEpisodeFinal streams the OPT-404 episode-level final.mp4 (or
// dub_track.wav for audio-only episodes). Returns 404 with a helpful
// message when ep_episode_merge has not yet written OutputRelPath OR
// when the file has been moved/deleted between merge and download.
func (s *Server) serveEpisodeFinal(c *gin.Context) {
	ep, ok := s.getEpisodeForTenant(c)
	if !ok {
		return
	}
	if ep.OutputRelPath == "" {
		respondError(c, stdhttp.StatusNotFound, "episode_output_missing",
			"episode has no merged output yet (ep_episode_merge not run)")
		return
	}
	abs, err := storage.SecureJoinUnderRoot(s.cfg.DataRoot, ep.OutputRelPath)
	if err != nil {
		respondError(c, stdhttp.StatusBadRequest, "invalid_path",
			"episode output path is outside data root")
		return
	}
	if _, err := os.Stat(abs); err != nil {
		respondError(c, stdhttp.StatusNotFound, "episode_output_missing",
			"episode output file not found on disk")
		return
	}
	c.Header("Content-Type", contentTypeForPath(abs))
	c.Header("Content-Disposition",
		"inline; filename=\""+downloadFilenameForEpisode(ep.ID, ep.OutputRelPath)+"\"")
	c.Header("Cache-Control", "private, max-age=60")
	c.File(abs)
}

// serveEpisodeChaptersJSON streams the OPT-404 chapters.json manifest.
// Different from serveEpisodeFinal we always set
// Cache-Control: no-store because the manifest is mutable mid-pipeline
// and stale copies would mislead operators reading the current state.
func (s *Server) serveEpisodeChaptersJSON(c *gin.Context) {
	ep, ok := s.getEpisodeForTenant(c)
	if !ok {
		return
	}
	relPath := ep.ChaptersManifestRelPath
	if relPath == "" {
		// Fall back to the canonical path even when the DB row hasn't
		// been stamped yet — the manifest may exist on disk if the back-
		// fill ran but the UpdateEpisodeOutput call failed (rare race).
		relPath = ep.GetChaptersJSONRelPath()
	}
	abs, err := storage.SecureJoinUnderRoot(s.cfg.DataRoot, relPath)
	if err != nil {
		respondError(c, stdhttp.StatusBadRequest, "invalid_path",
			"manifest path is outside data root")
		return
	}
	if _, err := os.Stat(abs); err != nil {
		respondError(c, stdhttp.StatusNotFound, "chapters_manifest_missing",
			"chapters.json not found (ep_episode_merge has not run, or back-fill pending)")
		return
	}
	c.Header("Content-Type", "application/json; charset=utf-8")
	c.Header("Cache-Control", "no-store")
	c.File(abs)
}

// serveJobFinal streams the chapter-level final video/audio at
// Job.OutputRelPath. Works uniformly for both layouts because we read
// the path from the DB rather than constructing it (see
// lessons-learned.mdc rule 1).
func (s *Server) serveJobFinal(c *gin.Context) {
	job, ok := s.getJobForTenant(c)
	if !ok {
		return
	}
	if job.OutputRelPath == "" {
		respondError(c, stdhttp.StatusNotFound, "job_output_missing",
			"job has no merged output yet (stage_merge not run)")
		return
	}
	abs, err := storage.SecureJoinUnderRoot(s.cfg.DataRoot, job.OutputRelPath)
	if err != nil {
		respondError(c, stdhttp.StatusBadRequest, "invalid_path",
			"job output path is outside data root")
		return
	}
	if _, err := os.Stat(abs); err != nil {
		respondError(c, stdhttp.StatusNotFound, "job_output_missing",
			"job output file not found on disk")
		return
	}
	c.Header("Content-Type", contentTypeForPath(abs))
	c.Header("Content-Disposition",
		"inline; filename=\""+downloadFilenameForJob(job.ID, job.ChapterOrdinal, job.OutputRelPath)+"\"")
	c.Header("Cache-Control", "private, max-age=60")
	c.File(abs)
}

// downloadFilenameForEpisode picks a download filename for the episode
// final. Format: episode-{id}-final{.ext}. Keeps the OS extension from
// the relpath so audio-only episodes (.wav) don't get muxed-as-mp4.
func downloadFilenameForEpisode(episodeID uint, relPath string) string {
	ext := strings.ToLower(filepath.Ext(relPath))
	if ext == "" {
		ext = ".mp4"
	}
	return "episode-" + uintToA(episodeID) + "-final" + ext
}

// downloadFilenameForJob is the chapter-level analogue.
// Format: job-{id}-ch{ordinal:02d}{.ext}.
func downloadFilenameForJob(jobID uint, ordinal int, relPath string) string {
	ext := strings.ToLower(filepath.Ext(relPath))
	if ext == "" {
		ext = ".mp4"
	}
	if ordinal < 1 {
		ordinal = 1
	}
	return "job-" + uintToA(jobID) + "-ch" + twoDigit(ordinal) + ext
}

func contentTypeForPath(path string) string {
	switch strings.ToLower(filepath.Ext(path)) {
	case ".mp4", ".m4v":
		return "video/mp4"
	case ".mkv":
		return "video/x-matroska"
	case ".webm":
		return "video/webm"
	case ".wav":
		return "audio/wav"
	case ".m4a":
		return "audio/mp4"
	case ".mp3":
		return "audio/mpeg"
	case ".json":
		return "application/json; charset=utf-8"
	default:
		return "application/octet-stream"
	}
}

func uintToA(v uint) string {
	if v == 0 {
		return "0"
	}
	var b [20]byte
	pos := len(b)
	for v > 0 {
		pos--
		b[pos] = byte('0' + (v % 10))
		v /= 10
	}
	return string(b[pos:])
}

func twoDigit(n int) string {
	if n < 0 {
		n = -n
	}
	if n < 10 {
		return "0" + uintToA(uint(n))
	}
	return uintToA(uint(n))
}
