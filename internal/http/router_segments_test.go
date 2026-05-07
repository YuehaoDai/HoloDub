package http

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	stdhttp "net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"holodub/internal/config"
	"holodub/internal/models"
	"holodub/internal/store"

	"github.com/gin-gonic/gin"
	"gorm.io/driver/sqlite"
	"gorm.io/gorm"
)

// newPatchSegmentTestEnv builds a minimal gin engine that wires only the
// PATCH /jobs/:id/segments/:segmentId route against an in-memory sqlite
// store.  The pipeline pointer is intentionally nil because the src_text
// branch never calls into it; if a future refactor changes that we want a
// loud nil-pointer panic in the test rather than a silent skip.
func newPatchSegmentTestEnv(t *testing.T) (*gin.Engine, *store.Store, uint, uint) {
	t.Helper()
	gin.SetMode(gin.TestMode)

	db, err := gorm.Open(sqlite.Open("file::memory:?cache=shared"), &gorm.Config{})
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	// store.NewWithDB lets us bypass the config/DSN plumbing of store.New.
	st := store.NewWithDB(db)

	if err := st.AutoMigrate(); err != nil {
		t.Fatalf("auto-migrate: %v", err)
	}

	ctx := context.Background()
	job := &models.Job{
		Name:           "patch-test",
		Status:         models.JobStatusAwaitingReview,
		CurrentStage:   models.StageSegmentReview,
		SourceLanguage: "ja",
		TargetLanguage: "zh",
	}
	if err := st.CreateJob(ctx, job); err != nil {
		t.Fatalf("create job: %v", err)
	}
	if err := st.ReplaceSegments(ctx, job.ID, []models.SegmentDraft{{
		StartMs:      1000,
		EndMs:        3500,
		Text:         "原始 ASR 文本",
		SpeakerLabel: "SPK_01",
	}}); err != nil {
		t.Fatalf("seed segment: %v", err)
	}
	segs, err := st.ListSegments(ctx, job.ID, nil)
	if err != nil || len(segs) != 1 {
		t.Fatalf("list segments: err=%v len=%d", err, len(segs))
	}

	server := &Server{
		cfg:      config.Config{DefaultTenantKey: "default"},
		store:    st,
		pipeline: nil,
	}
	r := gin.New()
	r.Use(func(c *gin.Context) {
		c.Set(tenantContextKey, "default")
		c.Set(requestIDContextKey, "test-req")
		c.Next()
	})
	r.PATCH("/jobs/:id/segments/:segmentId", server.patchSegment)
	return r, st, job.ID, segs[0].ID
}

func TestPatchSegment_SrcText_OK(t *testing.T) {
	r, st, jobID, segID := newPatchSegmentTestEnv(t)

	body := `{"src_text": "  用户修正后的文本  "}`
	req := httptest.NewRequest("PATCH", patchURL(jobID, segID), strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)

	if rec.Code != stdhttp.StatusOK {
		t.Fatalf("expected 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	var resp map[string]any
	_ = json.Unmarshal(rec.Body.Bytes(), &resp)
	if resp["src_text"] != "用户修正后的文本" {
		t.Fatalf("src_text not trimmed in response: %v", resp["src_text"])
	}

	seg, err := st.GetSegment(context.Background(), segID)
	if err != nil {
		t.Fatalf("get segment: %v", err)
	}
	if seg.SourceText != "用户修正后的文本" {
		t.Fatalf("DB src_text not updated: got %q", seg.SourceText)
	}
	if seg.StartMs != 1000 || seg.EndMs != 3500 {
		t.Fatalf("timing changed unexpectedly: %d/%d", seg.StartMs, seg.EndMs)
	}
	if seg.Status != models.SegmentStatusPending {
		t.Fatalf("status changed unexpectedly: %q", seg.Status)
	}
}

func TestPatchSegment_SrcText_TableDriven(t *testing.T) {
	cases := []struct {
		name       string
		bodyOf     func(jobID, segID uint) string
		jobStatus  models.JobStatus
		wantStatus int
		wantCode   string
	}{
		{
			name:       "wrong job status (running) returns 409",
			bodyOf:     func(_, _ uint) string { return `{"src_text": "新文本"}` },
			jobStatus:  models.JobStatusRunning,
			wantStatus: stdhttp.StatusConflict,
			wantCode:   "job_not_in_awaiting_review",
		},
		{
			name:       "empty after trim returns 400",
			bodyOf:     func(_, _ uint) string { return `{"src_text": "    "}` },
			jobStatus:  models.JobStatusAwaitingReview,
			wantStatus: stdhttp.StatusBadRequest,
			wantCode:   "invalid_request",
		},
		{
			name: "overlong returns 400",
			bodyOf: func(_, _ uint) string {
				big := strings.Repeat("a", maxSegmentSourceTextBytes+1)
				return `{"src_text": "` + big + `"}`
			},
			jobStatus:  models.JobStatusAwaitingReview,
			wantStatus: stdhttp.StatusBadRequest,
			wantCode:   "invalid_request",
		},
		{
			name: "src_text combined with target_text returns 400",
			bodyOf: func(_, _ uint) string {
				return `{"src_text": "新文本", "target_text": "翻译"}`
			},
			jobStatus:  models.JobStatusAwaitingReview,
			wantStatus: stdhttp.StatusBadRequest,
			wantCode:   "invalid_request",
		},
		{
			name: "src_text combined with rerun returns 400",
			bodyOf: func(_, _ uint) string {
				return `{"src_text": "新文本", "rerun": true}`
			},
			jobStatus:  models.JobStatusAwaitingReview,
			wantStatus: stdhttp.StatusBadRequest,
			wantCode:   "invalid_request",
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r, st, jobID, segID := newPatchSegmentTestEnv(t)
			if tc.jobStatus != models.JobStatusAwaitingReview {
				if err := st.UpdateJobState(context.Background(), jobID, tc.jobStatus, models.StageTranslate, "", false); err != nil {
					t.Fatalf("update job state: %v", err)
				}
			}

			body := tc.bodyOf(jobID, segID)
			req := httptest.NewRequest("PATCH", patchURL(jobID, segID), strings.NewReader(body))
			req.Header.Set("Content-Type", "application/json")
			rec := httptest.NewRecorder()
			r.ServeHTTP(rec, req)

			if rec.Code != tc.wantStatus {
				t.Fatalf("status: want %d got %d body=%s", tc.wantStatus, rec.Code, rec.Body.String())
			}
			var resp errorResponse
			if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
				t.Fatalf("unmarshal err: %v body=%s", err, rec.Body.String())
			}
			if resp.Code != tc.wantCode {
				t.Fatalf("error code: want %q got %q", tc.wantCode, resp.Code)
			}

			seg, _ := st.GetSegment(context.Background(), segID)
			if seg.SourceText != "原始 ASR 文本" {
				t.Fatalf("source_text leaked through failure path: %q", seg.SourceText)
			}
		})
	}
}

func TestPatchSegment_SrcText_WrongSegmentReturnsNotFound(t *testing.T) {
	r, _, jobID, _ := newPatchSegmentTestEnv(t)

	body := `{"src_text": "新文本"}`
	req := httptest.NewRequest("PATCH", patchURL(jobID, 999999), strings.NewReader(body))
	req.Header.Set("Content-Type", "application/json")
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)

	if rec.Code != stdhttp.StatusNotFound {
		t.Fatalf("expected 404, got %d body=%s", rec.Code, rec.Body.String())
	}
	var resp errorResponse
	_ = json.Unmarshal(rec.Body.Bytes(), &resp)
	if resp.Code != "segment_not_found" {
		t.Fatalf("expected segment_not_found, got %q", resp.Code)
	}
}

// patchURL is a tiny helper so the URL shape is in one place.
func patchURL(jobID, segID uint) string {
	return "/jobs/" + uintStr(jobID) + "/segments/" + uintStr(segID)
}

func uintStr(v uint) string {
	var buf [20]byte
	i := len(buf)
	if v == 0 {
		return "0"
	}
	for v > 0 {
		i--
		buf[i] = byte('0' + v%10)
		v /= 10
	}
	return string(buf[i:])
}

// drainResp is a debugging helper retained for failure inspection.
func drainResp(rec *httptest.ResponseRecorder) string {
	b, _ := io.ReadAll(bytes.NewReader(rec.Body.Bytes()))
	return string(b)
}

var _ = drainResp // keep helper alive for ad-hoc debugging
