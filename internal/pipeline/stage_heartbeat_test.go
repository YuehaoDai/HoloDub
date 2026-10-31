package pipeline

import (
	"context"
	"strings"
	"testing"
	"time"

	"holodub/internal/config"
	"holodub/internal/models"
	"holodub/internal/store"
)

func TestStageHeartbeatRefreshesLongRunningJob(t *testing.T) {
	st, err := store.New(config.Config{
		DatabaseDriver: "sqlite",
		DatabaseDSN:    "file:stage-heartbeat?mode=memory&cache=shared",
	})
	if err != nil {
		if strings.Contains(strings.ToLower(err.Error()), "cgo") {
			t.Skip("sqlite driver requires cgo in this environment")
		}
		t.Fatalf("new store: %v", err)
	}
	if err := st.AutoMigrate(); err != nil {
		t.Fatalf("auto migrate: %v", err)
	}

	job := &models.Job{
		Name:           "heartbeat",
		Status:         models.JobStatusRunning,
		CurrentStage:   models.StageSeparate,
		SourceLanguage: "en",
		TargetLanguage: "zh-CN",
	}
	if err := st.CreateJob(context.Background(), job); err != nil {
		t.Fatalf("create job: %v", err)
	}
	before, err := st.GetJob(context.Background(), job.ID)
	if err != nil {
		t.Fatalf("get job before heartbeat: %v", err)
	}
	if before.HeartbeatAt != nil {
		t.Fatalf("expected empty initial heartbeat, got %v", before.HeartbeatAt)
	}

	oldInterval := stageHeartbeatInterval
	oldTimeout := stageHeartbeatUpdateTimeout
	stageHeartbeatInterval = 10 * time.Millisecond
	stageHeartbeatUpdateTimeout = time.Second
	defer func() {
		stageHeartbeatInterval = oldInterval
		stageHeartbeatUpdateTimeout = oldTimeout
	}()

	svc := &Service{store: st}
	stop := svc.startStageHeartbeat(job.ID, models.StageSeparate)
	defer stop()

	deadline := time.Now().Add(2 * time.Second)
	for time.Now().Before(deadline) {
		got, err := st.GetJob(context.Background(), job.ID)
		if err != nil {
			// Shared-cache SQLite reports SQLITE_LOCKED instead of waiting
			// while the heartbeat goroutine writes the same table.
			if strings.Contains(err.Error(), "locked") {
				time.Sleep(20 * time.Millisecond)
				continue
			}
			t.Fatalf("get job during heartbeat: %v", err)
		}
		if got.HeartbeatAt != nil {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatal("heartbeat was not refreshed")
}
