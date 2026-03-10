package queue

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"holodub/internal/config"
	"holodub/internal/models"

	"github.com/redis/go-redis/v9"
)

type Queue struct {
	client           *redis.Client
	key              string
	delayedKey       string
	deadLetterKey    string
	stageLeasePrefix string
}

func New(cfg config.Config) *Queue {
	client := redis.NewClient(&redis.Options{
		Addr:     cfg.RedisAddr,
		Password: cfg.RedisPassword,
		DB:       cfg.RedisDB,
	})
	return &Queue{
		client:           client,
		key:              cfg.QueueKey,
		delayedKey:       cfg.DelayedQueueKey,
		deadLetterKey:    cfg.DeadLetterQueueKey,
		stageLeasePrefix: cfg.StageLeasePrefix,
	}
}

func (q *Queue) Ping(ctx context.Context) error {
	return q.client.Ping(ctx).Err()
}

func (q *Queue) Enqueue(ctx context.Context, task models.TaskPayload) error {
	payload, err := json.Marshal(task)
	if err != nil {
		return fmt.Errorf("marshal task: %w", err)
	}
	return q.client.RPush(ctx, q.key, payload).Err()
}

func (q *Queue) EnqueueWithDelay(ctx context.Context, task models.TaskPayload, delay time.Duration) error {
	payload, err := json.Marshal(task)
	if err != nil {
		return fmt.Errorf("marshal delayed task: %w", err)
	}
	score := float64(time.Now().Add(delay).UnixMilli())
	return q.client.ZAdd(ctx, q.delayedKey, redis.Z{
		Score:  score,
		Member: string(payload),
	}).Err()
}

func (q *Queue) PopBlocking(ctx context.Context, timeout time.Duration) (*models.TaskPayload, error) {
	result, err := q.client.BLPop(ctx, timeout, q.key).Result()
	if err != nil {
		if err == redis.Nil {
			return nil, nil
		}
		return nil, err
	}
	if len(result) != 2 {
		return nil, fmt.Errorf("unexpected BLPOP response length %d", len(result))
	}
	var task models.TaskPayload
	if err := json.Unmarshal([]byte(result[1]), &task); err != nil {
		return nil, fmt.Errorf("decode task payload: %w", err)
	}
	return &task, nil
}

func (q *Queue) PromoteDueDelayed(ctx context.Context, limit int64) error {
	maxScore := fmt.Sprintf("%d", time.Now().UnixMilli())
	members, err := q.client.ZRangeByScore(ctx, q.delayedKey, &redis.ZRangeBy{
		Min:    "-inf",
		Max:    maxScore,
		Offset: 0,
		Count:  limit,
	}).Result()
	if err != nil || len(members) == 0 {
		return err
	}

	pipe := q.client.TxPipeline()
	for _, member := range members {
		pipe.RPush(ctx, q.key, member)
		pipe.ZRem(ctx, q.delayedKey, member)
	}
	_, err = pipe.Exec(ctx)
	return err
}

func (q *Queue) EnqueueDeadLetter(ctx context.Context, task models.TaskPayload, errMsg string) error {
	payload := map[string]any{
		"failed_at": time.Now().UTC(),
		"task":      task,
		"error":     errMsg,
	}
	raw, err := json.Marshal(payload)
	if err != nil {
		return fmt.Errorf("marshal dead letter: %w", err)
	}
	return q.client.RPush(ctx, q.deadLetterKey, raw).Err()
}

func (q *Queue) AcquireStageLease(ctx context.Context, jobID uint, stage models.JobStage, ttl time.Duration) (bool, error) {
	key := q.stageLeaseKey(jobID, stage)
	return q.client.SetNX(ctx, key, time.Now().UTC().Format(time.RFC3339Nano), ttl).Result()
}

func (q *Queue) ReleaseStageLease(ctx context.Context, jobID uint, stage models.JobStage) error {
	return q.client.Del(ctx, q.stageLeaseKey(jobID, stage)).Err()
}

func (q *Queue) stageLeaseKey(jobID uint, stage models.JobStage) string {
	return fmt.Sprintf("%s:job:%d:stage:%s", q.stageLeasePrefix, jobID, stage)
}
