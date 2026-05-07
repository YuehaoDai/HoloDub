package http

// router_segments.go: HTTP handlers for the segment_review stage UI
// (suggestions accept/reject + manual merge/split + segmentation
// confirmation + ASR retry). Extracted from router.go to keep that
// file under 1k lines.

import (
	stdhttp "net/http"
	"strings"

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

