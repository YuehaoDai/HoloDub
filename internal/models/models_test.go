package models

import "testing"

func TestNextStage(t *testing.T) {
	next, ok := StageTranslate.Next()
	if !ok {
		t.Fatalf("expected translate to have a next stage")
	}
	if next != StageTTSDuration {
		t.Fatalf("expected next stage to be %q, got %q", StageTTSDuration, next)
	}
}
