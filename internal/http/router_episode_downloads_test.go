//go:build cgo
// +build cgo

// Tests for the OPT-403/404 download handlers added in
// router_episode_downloads.go.
//
// NOTE: tagged `cgo` because the sqlite-in-memory store driver requires
// CGO; on Windows go test typically runs with CGO_ENABLED=0 which makes
// gorm.io/driver/sqlite a stub that errors out at gorm.Open. Linux CI
// builds run with cgo enabled and exercise these tests normally. The
// pure-function helpers (downloadFilenameForEpisode etc.) are exercised
// in the no-cgo file below so coverage exists everywhere.
//
// The handlers are thin: they read a relpath from a DB row, resolve it
// under DataRoot, and stream the file with appropriate headers. The
// scope of these tests is therefore the handler-level wiring:
//
//   - 404 + clean error code when the relpath column is empty
//     (ep_episode_merge / stage_merge has not yet written it)
//   - 404 + clean error code when the column has a path but the file
//     is not on disk (post-write tampering / hardlink miss)
//   - 200 + correct Content-Type / Content-Disposition + body bytes
//     for the happy path
//   - filename helpers cover audio (.wav) and video (.mp4) extensions
//     so audio-only episodes don't get muxed-as-mp4 download names
package http

import (
	"context"
	stdhttp "net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"testing"

	"holodub/internal/config"
	"holodub/internal/models"
	"holodub/internal/store"

	"github.com/gin-gonic/gin"
	"gorm.io/driver/sqlite"
	"gorm.io/gorm"
)

// newDownloadsTestEnv spins up a minimal server with the three OPT-403
// download routes wired against an in-memory store + a temp DataRoot.
// Returns (router, store, dataRoot) so tests can seed Episode + Job rows
// AND drop expected files at the right relpath.
func newDownloadsTestEnv(t *testing.T) (*gin.Engine, *store.Store, string) {
	t.Helper()
	gin.SetMode(gin.TestMode)
	dataRoot := t.TempDir()

	db, err := gorm.Open(sqlite.Open("file::memory:?cache=shared"), &gorm.Config{})
	if err != nil {
		t.Fatalf("open sqlite: %v", err)
	}
	st := store.NewWithDB(db)
	if err := st.AutoMigrate(); err != nil {
		t.Fatalf("auto-migrate: %v", err)
	}

	server := &Server{
		cfg:      config.Config{DefaultTenantKey: "default", DataRoot: dataRoot},
		store:    st,
		pipeline: nil,
	}
	r := gin.New()
	r.Use(func(c *gin.Context) {
		c.Set(tenantContextKey, "default")
		c.Set(requestIDContextKey, "test-req")
		c.Next()
	})
	r.GET("/episodes/:id/download/final", server.serveEpisodeFinal)
	r.GET("/episodes/:id/chapters.json", server.serveEpisodeChaptersJSON)
	r.GET("/jobs/:id/download/final", server.serveJobFinal)
	return r, st, dataRoot
}

func writeFile(t *testing.T, root, relPath string, body []byte) {
	t.Helper()
	abs := filepath.Join(root, filepath.FromSlash(relPath))
	if err := os.MkdirAll(filepath.Dir(abs), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(abs, body, 0o644); err != nil {
		t.Fatalf("write %s: %v", abs, err)
	}
}

func TestServeEpisodeFinal_404WhenOutputRelPathEmpty(t *testing.T) {
	r, st, _ := newDownloadsTestEnv(t)

	ep := &models.Episode{Name: "Ep 1"}
	if err := st.CreateEpisode(context.Background(), ep); err != nil {
		t.Fatalf("create episode: %v", err)
	}

	req := httptest.NewRequest("GET", "/episodes/1/download/final", nil)
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)

	if rec.Code != stdhttp.StatusNotFound {
		t.Fatalf("want 404, got %d body=%s", rec.Code, rec.Body.String())
	}
	if !contains(rec.Body.String(), "episode_output_missing") {
		t.Errorf("want error code episode_output_missing, got %s", rec.Body.String())
	}
}

func TestServeEpisodeFinal_404WhenFileMissing(t *testing.T) {
	r, st, _ := newDownloadsTestEnv(t)

	ep := &models.Episode{Name: "Ep 1", OutputRelPath: "episodes/1/output/vp0/final.mp4"}
	if err := st.CreateEpisode(context.Background(), ep); err != nil {
		t.Fatalf("create episode: %v", err)
	}
	// Note: do NOT writeFile — file should be absent on disk.

	req := httptest.NewRequest("GET", "/episodes/1/download/final", nil)
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)

	if rec.Code != stdhttp.StatusNotFound {
		t.Fatalf("want 404 for missing file, got %d", rec.Code)
	}
}

func TestServeEpisodeFinal_HappyPath(t *testing.T) {
	r, st, root := newDownloadsTestEnv(t)

	body := []byte("synthetic mp4 payload")
	rel := "episodes/1/output/vp0/final.mp4"
	writeFile(t, root, rel, body)

	ep := &models.Episode{Name: "Ep 1", OutputRelPath: rel}
	if err := st.CreateEpisode(context.Background(), ep); err != nil {
		t.Fatalf("create episode: %v", err)
	}

	req := httptest.NewRequest("GET", "/episodes/1/download/final", nil)
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)

	if rec.Code != stdhttp.StatusOK {
		t.Fatalf("want 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	if rec.Body.String() != string(body) {
		t.Fatalf("body drift: %q != %q", rec.Body.String(), string(body))
	}
	if got := rec.Header().Get("Content-Type"); got != "video/mp4" {
		t.Errorf("Content-Type: want video/mp4, got %q", got)
	}
	if !contains(rec.Header().Get("Content-Disposition"), "episode-1-final.mp4") {
		t.Errorf("Content-Disposition should include filename: %q",
			rec.Header().Get("Content-Disposition"))
	}
}

func TestServeJobFinal_HappyPath(t *testing.T) {
	r, st, root := newDownloadsTestEnv(t)

	body := []byte("chapter mp4 payload")
	rel := "episodes/1/chapters/vp0/ch01.mp4"
	writeFile(t, root, rel, body)

	job := &models.Job{
		Name:           "ch1",
		Status:         models.JobStatusCompleted,
		OutputRelPath:  rel,
		ChapterOrdinal: 1,
		SourceLanguage: "en",
		TargetLanguage: "zh",
	}
	if err := st.CreateJob(context.Background(), job); err != nil {
		t.Fatalf("create job: %v", err)
	}

	req := httptest.NewRequest("GET",
		"/jobs/"+uintToA(job.ID)+"/download/final", nil)
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)

	if rec.Code != stdhttp.StatusOK {
		t.Fatalf("want 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	if rec.Body.String() != string(body) {
		t.Fatalf("body drift: %q != %q", rec.Body.String(), string(body))
	}
	if !contains(rec.Header().Get("Content-Disposition"), "job-"+uintToA(job.ID)+"-ch01.mp4") {
		t.Errorf("Content-Disposition: %q", rec.Header().Get("Content-Disposition"))
	}
}

func TestServeJobFinal_404OnEmptyOutputRelPath(t *testing.T) {
	r, st, _ := newDownloadsTestEnv(t)
	job := &models.Job{Name: "ch1", Status: models.JobStatusRunning,
		SourceLanguage: "en", TargetLanguage: "zh"}
	if err := st.CreateJob(context.Background(), job); err != nil {
		t.Fatalf("create job: %v", err)
	}
	req := httptest.NewRequest("GET",
		"/jobs/"+uintToA(job.ID)+"/download/final", nil)
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)
	if rec.Code != stdhttp.StatusNotFound {
		t.Fatalf("want 404, got %d", rec.Code)
	}
	if !contains(rec.Body.String(), "job_output_missing") {
		t.Errorf("want job_output_missing code, got %s", rec.Body.String())
	}
}

func TestServeEpisodeChaptersJSON_HappyPath(t *testing.T) {
	r, st, root := newDownloadsTestEnv(t)

	rel := "episodes/1/chapters.json"
	body := []byte(`{"schema_version":1,"episode_id":1,"chapters":[]}`)
	writeFile(t, root, rel, body)

	ep := &models.Episode{Name: "Ep 1", ChaptersManifestRelPath: rel}
	if err := st.CreateEpisode(context.Background(), ep); err != nil {
		t.Fatalf("create episode: %v", err)
	}

	req := httptest.NewRequest("GET", "/episodes/1/chapters.json", nil)
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)

	if rec.Code != stdhttp.StatusOK {
		t.Fatalf("want 200, got %d body=%s", rec.Code, rec.Body.String())
	}
	if got := rec.Header().Get("Content-Type"); got != "application/json; charset=utf-8" {
		t.Errorf("Content-Type: want application/json; charset=utf-8, got %q", got)
	}
	if got := rec.Header().Get("Cache-Control"); got != "no-store" {
		t.Errorf("Cache-Control should be no-store; got %q", got)
	}
}

func contains(s, sub string) bool {
	for i := 0; i+len(sub) <= len(s); i++ {
		if s[i:i+len(sub)] == sub {
			return true
		}
	}
	return false
}
