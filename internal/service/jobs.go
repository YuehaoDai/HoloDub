// Package service is the use-case layer of the Go control plane.
//
// Today most business logic still lives in two places:
//
//   - internal/pipeline.Service   (stage orchestration, queue I/O)
//   - internal/store.Store        (CRUD)
//
// Handlers in internal/http call both directly. That worked fine while the
// surface was small but produces 1300+ line router.go files where it is
// hard to mock a single dependency for a unit test.
//
// JobService is the first slice of the use-case layer. Each method wraps a
// single user-visible operation (start a job, retry a stage, …) and the
// HTTP handler shrinks to: bind -> validate -> service.X(ctx, args) ->
// respondError. Subsequent PRs migrate handlers one at a time, leaving the
// facade as the only seam new code reaches for.
package service

import (
	"context"

	"holodub/internal/models"
	"holodub/internal/pipeline"
	"holodub/internal/store"
)

// JobsAPI is the contract HTTP handlers depend on. Defining it as an
// interface lets handler tests use a fake implementation without standing
// up Postgres + Redis.
type JobsAPI interface {
	Start(ctx context.Context, jobID uint, requestedBy string) error
	Retry(ctx context.Context, jobID uint, stage models.JobStage, segmentIDs []uint, requestedBy string) error
	Cancel(ctx context.Context, jobID uint) error
	ConfirmSegmentation(ctx context.Context, jobID uint, requestedBy string) error
	RetryASR(ctx context.Context, jobID uint, requestedBy string) error
}

// JobService is the production implementation backed by pipeline.Service
// and store.Store.
type JobService struct {
	pipeline *pipeline.Service
	store    *store.Store
}

// NewJobService constructs a JobService.
func NewJobService(pipelineSvc *pipeline.Service, st *store.Store) *JobService {
	return &JobService{pipeline: pipelineSvc, store: st}
}

func (s *JobService) Start(ctx context.Context, jobID uint, requestedBy string) error {
	return s.pipeline.StartJob(ctx, jobID, requestedBy)
}

func (s *JobService) Retry(ctx context.Context, jobID uint, stage models.JobStage, segmentIDs []uint, requestedBy string) error {
	return s.pipeline.RetryJob(ctx, jobID, stage, segmentIDs, requestedBy)
}

func (s *JobService) Cancel(ctx context.Context, jobID uint) error {
	return s.store.RequestJobCancel(ctx, jobID)
}

func (s *JobService) ConfirmSegmentation(ctx context.Context, jobID uint, requestedBy string) error {
	return s.pipeline.ConfirmSegmentation(ctx, jobID, requestedBy)
}

func (s *JobService) RetryASR(ctx context.Context, jobID uint, requestedBy string) error {
	return s.pipeline.RetryASR(ctx, jobID, requestedBy)
}
