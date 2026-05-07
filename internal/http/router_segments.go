package http

// router_segments.go: HTTP handlers for the segment_review stage UI
// (suggestions accept/reject + manual merge/split + segmentation
// confirmation + ASR retry). Extracted from router.go to keep that
// file under 1k lines.

import (
	"errors"
	stdhttp "net/http"
	"strings"

	"holodub/internal/pipeline"

	"github.com/gin-gonic/gin"
)

// maxSegmentSourceTextBytes caps the manually edited ASR transcript size to
// guard against runaway payloads.  8 KiB comfortably covers any realistic
// single-segment utterance (typically <1 KiB) without being so large that a
// hostile client can pin database or LLM prompt budgets.
const maxSegmentSourceTextBytes = 8 * 1024

func (s *Server) listSegmentSuggestions(c *gin.Context) {
	jobID, err := parseID(c, "id")
	if err != nil {
		return
	}
	suggestions, err := s.store.ListSuggestions(c.Request.Context(), jobID)
	if err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "list_suggestions_failed", err.Error())
		return
	}
	c.JSON(stdhttp.StatusOK, gin.H{"suggestions": suggestions})
}

func (s *Server) acceptSegmentSuggestion(c *gin.Context) {
	jobID, err := parseID(c, "id")
	if err != nil {
		return
	}
	sugID, err := parseID(c, "sid")
	if err != nil {
		return
	}
	ctx := c.Request.Context()

	// Fetch the suggestion
	sug, err := s.store.GetSuggestion(ctx, sugID)
	if err != nil {
		respondError(c, stdhttp.StatusNotFound, "suggestion_not_found", "suggestion not found")
		return
	}
	if sug.JobID != jobID {
		respondError(c, stdhttp.StatusNotFound, "suggestion_not_found", "suggestion not found")
		return
	}
	if sug.Status != "pending" {
		// Idempotent: already processed
		c.JSON(stdhttp.StatusOK, gin.H{"suggestion": sug})
		return
	}

	// Apply the action
	if sug.Action == "merge" {
		ids := make([]uint, len(sug.SegmentIDs))
		for i, v := range sug.SegmentIDs {
			ids[i] = v
		}
		if err := s.store.MergeSegments(ctx, jobID, ids); err != nil {
			respondError(c, stdhttp.StatusBadRequest, "merge_failed", err.Error())
			return
		}
	}
	// Mark suggestion accepted
	if err := s.store.UpdateSuggestionStatus(ctx, sugID, "accepted"); err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "update_suggestion_failed", err.Error())
		return
	}
	c.JSON(stdhttp.StatusOK, gin.H{"ok": true})
}

func (s *Server) rejectSegmentSuggestion(c *gin.Context) {
	jobID, err := parseID(c, "id")
	if err != nil {
		return
	}
	sugID, err := parseID(c, "sid")
	if err != nil {
		return
	}
	ctx := c.Request.Context()
	sug, err := s.store.GetSuggestion(ctx, sugID)
	if err != nil {
		respondError(c, stdhttp.StatusNotFound, "suggestion_not_found", "suggestion not found")
		return
	}
	if sug.JobID != jobID {
		respondError(c, stdhttp.StatusNotFound, "suggestion_not_found", "suggestion not found")
		return
	}
	if err := s.store.UpdateSuggestionStatus(ctx, sugID, "rejected"); err != nil {
		respondError(c, stdhttp.StatusInternalServerError, "update_suggestion_failed", err.Error())
		return
	}
	c.JSON(stdhttp.StatusOK, gin.H{"ok": true})
}

func (s *Server) mergeSegments(c *gin.Context) {
	jobID, err := parseID(c, "id")
	if err != nil {
		return
	}
	var req struct {
		SegmentIDs []uint `json:"segment_ids" binding:"required"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		respondError(c, stdhttp.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	if len(req.SegmentIDs) < 2 {
		respondError(c, stdhttp.StatusBadRequest, "invalid_request", "at least 2 segment_ids required")
		return
	}
	if err := s.store.MergeSegments(c.Request.Context(), jobID, req.SegmentIDs); err != nil {
		respondError(c, stdhttp.StatusBadRequest, "merge_failed", err.Error())
		return
	}
	c.JSON(stdhttp.StatusOK, gin.H{"ok": true})
}

func (s *Server) splitSegment(c *gin.Context) {
	jobID, err := parseID(c, "id")
	if err != nil {
		return
	}
	segID, err := parseID(c, "segmentId")
	if err != nil {
		return
	}
	var req struct {
		SplitCharIndex int `json:"split_char_index" binding:"required"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		respondError(c, stdhttp.StatusBadRequest, "invalid_request", err.Error())
		return
	}
	if err := s.store.SplitSegment(c.Request.Context(), jobID, segID, req.SplitCharIndex); err != nil {
		respondError(c, stdhttp.StatusBadRequest, "split_failed", err.Error())
		return
	}
	c.JSON(stdhttp.StatusOK, gin.H{"ok": true})
}

func (s *Server) confirmSegmentation(c *gin.Context) {
	jobID, err := parseID(c, "id")
	if err != nil {
		return
	}
	if err := s.pipeline.ConfirmSegmentation(c.Request.Context(), jobID, "user"); err != nil {
		status := stdhttp.StatusInternalServerError
		if strings.Contains(err.Error(), "not in awaiting_review") {
			status = stdhttp.StatusConflict
		}
		respondError(c, status, "confirm_failed", err.Error())
		return
	}
	c.JSON(stdhttp.StatusOK, gin.H{"ok": true})
}

func (s *Server) retryASR(c *gin.Context) {
	jobID, err := parseID(c, "id")
	if err != nil {
		return
	}
	if err := s.pipeline.RetryASR(c.Request.Context(), jobID, "user"); err != nil {
		status := stdhttp.StatusInternalServerError
		if strings.Contains(err.Error(), "not in awaiting_review") {
			status = stdhttp.StatusConflict
		}
		respondError(c, status, "retry_asr_failed", err.Error())
		return
	}
	c.JSON(stdhttp.StatusOK, gin.H{"ok": true})
}

// retrySegmentASR re-runs ASR on a single segment by clipping the chosen
// time window out of the job's vocals (or input) audio and feeding it
// through faster-whisper.  It is the per-segment counterpart of retryASR
// and preserves all other segments / suggestions / manual edits.
//
// Response shapes:
//   - 200 { updated: true, segment_id, src_text }      - persisted
//   - 200 { updated: false, segment_id, warning: ... } - empty transcript
//   - 404 segment_not_found / job_not_found             - missing data
//   - 409 job_not_in_awaiting_review                    - wrong job state
//   - 502 ml_transcribe_failed                          - upstream error
func (s *Server) retrySegmentASR(c *gin.Context) {
	jobID, err := parseID(c, "id")
	if err != nil {
		return
	}
	segmentID, ok := parseUintParam(c, "segmentId")
	if !ok {
		return
	}

	text, err := s.pipeline.RetrySegmentASR(c.Request.Context(), jobID, segmentID, "user")
	if err == nil {
		c.JSON(stdhttp.StatusOK, gin.H{
			"updated":    true,
			"segment_id": segmentID,
			"src_text":   text,
		})
		return
	}

	if errors.Is(err, pipeline.ErrSegmentTranscriptionEmpty) {
		c.JSON(stdhttp.StatusOK, gin.H{
			"updated":    false,
			"segment_id": segmentID,
			"warning":    "empty_transcription",
			"message":    "ASR returned no text for this window; please edit the transcript manually",
		})
		return
	}

	msg := err.Error()
	switch {
	case strings.Contains(msg, "not in awaiting_review"):
		respondError(c, stdhttp.StatusConflict, "job_not_in_awaiting_review", msg)
	case strings.Contains(msg, "does not belong to job"),
		strings.Contains(msg, "record not found"):
		respondError(c, stdhttp.StatusNotFound, "segment_not_found", msg)
	case strings.Contains(msg, "ml transcribe_segment"):
		respondError(c, stdhttp.StatusBadGateway, "ml_transcribe_failed", msg)
	default:
		respondError(c, stdhttp.StatusInternalServerError, "retry_segment_asr_failed", msg)
	}
}

