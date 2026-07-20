package http

import (
	stdhttp "net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"holodub/internal/config"

	"github.com/gin-gonic/gin"
)

func TestServeDataArtifactStreamsListeningPackHTML(t *testing.T) {
	gin.SetMode(gin.TestMode)
	dataRoot := t.TempDir()
	relPath := filepath.ToSlash(filepath.Join("training", "lora_runs", "pilot", "listening_pack", "index.html"))
	absPath := filepath.Join(dataRoot, filepath.FromSlash(relPath))
	if err := os.MkdirAll(filepath.Dir(absPath), 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	if err := os.WriteFile(absPath, []byte("<!doctype html><title>pack</title>"), 0o644); err != nil {
		t.Fatalf("write: %v", err)
	}

	server := &Server{cfg: config.Config{DataRoot: dataRoot}}
	r := gin.New()
	r.GET("/data-artifacts/*relpath", server.serveDataArtifact)

	req := httptest.NewRequest("GET", "/data-artifacts/data/"+relPath, nil)
	rec := httptest.NewRecorder()
	r.ServeHTTP(rec, req)

	if rec.Code != stdhttp.StatusOK {
		t.Fatalf("expected 200, got %d location=%s body=%s", rec.Code, rec.Header().Get("Location"), rec.Body.String())
	}
	if got := rec.Header().Get("Content-Type"); !strings.Contains(got, "text/html") {
		t.Fatalf("content type = %q", got)
	}
	if !strings.Contains(rec.Body.String(), "<title>pack</title>") {
		t.Fatalf("body mismatch: %q", rec.Body.String())
	}
}
