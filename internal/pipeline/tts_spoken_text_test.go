package pipeline

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"sync"
	"testing"

	"holodub/internal/config"
	"holodub/internal/ml"
	"holodub/internal/models"
	pipettstts "holodub/internal/pipeline/tts"
	"holodub/internal/store"

	"gorm.io/datatypes"
	"gorm.io/driver/sqlite"
	"gorm.io/gorm"
)

type capturedTTSRequest struct {
	Text              string         `json:"text"`
	TargetDurationSec float64        `json:"target_duration_sec"`
	VoiceConfig       map[string]any `json:"voice_config"`
	OutputRelPath     string         `json:"output_relpath"`
}

func TestProcessOneTTSSegmentLegacyNormalizesTextAndPersistsProvenance(t *testing.T) {
	ctx := context.Background()
	var captured capturedTTSRequest
	server := newTTSTestServer(t, &captured)
	st := newPipelineTestStore(t)
	job, segments := seedTTSJob(t, st, "jobs/7/vocals.wav", "服务器S3被选为leader")

	svc := &Service{
		cfg:   testTTSConfig(t.TempDir()),
		store: st,
		ml:    ml.New(server.URL),
	}
	if err := svc.processOneTTSSegmentWithHint(ctx, job, segments, 0, true, "", nil); err != nil {
		t.Fatalf("processOneTTSSegmentWithHint: %v", err)
	}

	if captured.Text != "三号节点被选为leader" {
		t.Fatalf("TTS text = %q, want normalized spoken text", captured.Text)
	}
	samples, _ := captured.VoiceConfig["sample_relpaths"].([]any)
	if len(samples) != 1 || samples[0] != "jobs/7/vocals.wav" {
		t.Fatalf("voice_config sample_relpaths = %#v, want job vocals fallback", captured.VoiceConfig["sample_relpaths"])
	}

	got, err := st.GetSegment(ctx, segments[0].ID)
	if err != nil {
		t.Fatalf("GetSegment: %v", err)
	}
	if got.TargetText != "服务器S3被选为leader" {
		t.Fatalf("TargetText = %q, want display text unchanged", got.TargetText)
	}
	assertTTSProvenance(t, got.Meta, "三号节点被选为leader", voiceConfigSourceJobVocals)
}

func TestRunSegmentAgentNormalizesTextAndPersistsProvenance(t *testing.T) {
	ctx := context.Background()
	var captured capturedTTSRequest
	server := newTTSTestServer(t, &captured)
	st := newPipelineTestStore(t)
	job, segments := seedTTSJob(t, st, "jobs/8/vocals.wav", "S1和S2同时成为leader")

	cfg := testTTSConfig(t.TempDir())
	cfg.SegmentAgentEnabled = true
	svc := &Service{
		cfg:   cfg,
		store: st,
		ml:    ml.New(server.URL),
	}
	if err := svc.runSegmentAgentV2WithHint(ctx, job, segments, 0, true, "", nil); err != nil {
		t.Fatalf("runSegmentAgentV2WithHint: %v", err)
	}

	if captured.Text != "一号节点和二号节点同时成为leader" {
		t.Fatalf("TTS text = %q, want normalized spoken text", captured.Text)
	}
	got, err := st.GetSegment(ctx, segments[0].ID)
	if err != nil {
		t.Fatalf("GetSegment: %v", err)
	}
	if got.TargetText != "S1和S2同时成为leader" {
		t.Fatalf("TargetText = %q, want display text unchanged", got.TargetText)
	}
	assertTTSProvenance(t, got.Meta, "一号节点和二号节点同时成为leader", voiceConfigSourceJobVocals)
}

func TestRunSegmentAgentKeepsTermHaoLabelAudible(t *testing.T) {
	ctx := context.Background()
	var captured capturedTTSRequest
	server := newTTSTestServer(t, &captured)
	st := newPipelineTestStore(t)
	job, segments := seedTTSJob(t, st, "jobs/term/vocals.wav", "命令对应的任期编号恰好就是5号任期。")

	cfg := testTTSConfig(t.TempDir())
	cfg.SegmentAgentEnabled = true
	svc := &Service{
		cfg:   cfg,
		store: st,
		ml:    ml.New(server.URL),
	}
	if err := svc.runSegmentAgentV2WithHint(ctx, job, segments, 0, true, "", nil); err != nil {
		t.Fatalf("runSegmentAgentV2WithHint: %v", err)
	}

	wantSpoken := "命令对应的任期编号恰好就是五 号任期。"
	if captured.Text != wantSpoken {
		t.Fatalf("TTS text = %q, want %q", captured.Text, wantSpoken)
	}
	got, err := st.GetSegment(ctx, segments[0].ID)
	if err != nil {
		t.Fatalf("GetSegment: %v", err)
	}
	assertTTSProvenance(t, got.Meta, wantSpoken, voiceConfigSourceJobVocals)
}

func TestPreviewVoiceNormalizesText(t *testing.T) {
	var captured capturedTTSRequest
	server := newTTSTestServer(t, &captured)
	svc := &Service{
		cfg: testTTSConfig(t.TempDir()),
		ml:  ml.New(server.URL),
	}
	profile := models.VoiceProfile{
		ID:             42,
		Name:           "preview",
		Provider:       "indextts2",
		SampleRelPaths: datatypes.JSON([]byte(`["voices/profile.wav"]`)),
	}
	seg := models.Segment{
		ID:         12,
		StartMs:    0,
		EndMs:      2000,
		TargetText: "S10向S21发送AppendEntries RPC",
	}
	if _, _, err := svc.PreviewVoice(context.Background(), 1, seg, profile); err != nil {
		t.Fatalf("PreviewVoice: %v", err)
	}
	if captured.Text != "十号节点向二十一号节点发送AppendEntries RPC" {
		t.Fatalf("preview TTS text = %q", captured.Text)
	}
}

func TestResolveVoiceConfigForSegmentSourceOrder(t *testing.T) {
	ctx := context.Background()
	st := newPipelineTestStore(t)
	job, segments := seedTTSJob(t, st, "jobs/9/vocals.wav", "普通文本")
	profile := models.VoiceProfile{
		Name:           "profile",
		Provider:       "indextts2",
		SampleRelPaths: datatypes.JSON([]byte(`["voices/profile.wav"]`)),
	}
	if err := st.CreateVoiceProfile(ctx, &profile); err != nil {
		t.Fatalf("CreateVoiceProfile: %v", err)
	}
	if err := st.UpdateSegmentVoice(ctx, segments[0].ID, profile.ID); err != nil {
		t.Fatalf("UpdateSegmentVoice: %v", err)
	}
	withProfile, err := st.GetSegment(ctx, segments[0].ID)
	if err != nil {
		t.Fatalf("GetSegment: %v", err)
	}
	svc := &Service{store: st}
	voiceConfig, resolved, source, err := svc.resolveVoiceConfigForSegment(ctx, job, *withProfile)
	if err != nil {
		t.Fatalf("resolve profile: %v", err)
	}
	if resolved == nil || resolved.ID != profile.ID || source != voiceConfigSourceProfile {
		t.Fatalf("profile resolution got profile=%v source=%q", resolved, source)
	}
	if samples := sampleRelPathsFromVoiceConfig(voiceConfig); len(samples) != 1 || samples[0] != "voices/profile.wav" {
		t.Fatalf("profile samples = %#v", samples)
	}

	noProfile := *withProfile
	noProfile.VoiceProfileID = nil
	voiceConfig, resolved, source, err = svc.resolveVoiceConfigForSegment(ctx, job, noProfile)
	if err != nil {
		t.Fatalf("resolve vocals fallback: %v", err)
	}
	if resolved != nil || source != voiceConfigSourceJobVocals {
		t.Fatalf("vocals fallback got profile=%v source=%q", resolved, source)
	}
	if samples := sampleRelPathsFromVoiceConfig(voiceConfig); len(samples) != 1 || samples[0] != "jobs/9/vocals.wav" {
		t.Fatalf("job vocals samples = %#v", samples)
	}

	ep := &models.Episode{
		Name:          "multi chapter voice fallback",
		Status:        models.EpisodeStatusDispatched,
		TotalChapters: 2,
	}
	if err := st.CreateEpisode(ctx, ep); err != nil {
		t.Fatalf("CreateEpisode: %v", err)
	}
	ch1 := &models.Job{
		Name:           "chapter 1",
		Status:         models.JobStatusCompleted,
		CurrentStage:   models.StageMerge,
		EpisodeID:      ep.ID,
		ChapterOrdinal: 1,
		VocalsRelPath:  "episodes/100/chapters/source/ch01.vocals.wav",
	}
	ch2 := &models.Job{
		Name:           "chapter 2",
		Status:         models.JobStatusCompleted,
		CurrentStage:   models.StageMerge,
		EpisodeID:      ep.ID,
		ChapterOrdinal: 2,
		VocalsRelPath:  "episodes/100/chapters/source/ch02.vocals.wav",
	}
	if err := st.CreateJob(ctx, ch1); err != nil {
		t.Fatalf("CreateJob ch1: %v", err)
	}
	if err := st.CreateJob(ctx, ch2); err != nil {
		t.Fatalf("CreateJob ch2: %v", err)
	}
	voiceConfig, resolved, source, err = svc.resolveVoiceConfigForSegment(ctx, ch1, noProfile)
	if err != nil {
		t.Fatalf("resolve episode vocals fallback: %v", err)
	}
	if resolved != nil || source != voiceConfigSourceEpisodeVocals {
		t.Fatalf("episode vocals fallback got profile=%v source=%q", resolved, source)
	}
	if samples := sampleRelPathsFromVoiceConfig(voiceConfig); len(samples) != 2 ||
		samples[0] != "episodes/100/chapters/source/ch01.vocals.wav" ||
		samples[1] != "episodes/100/chapters/source/ch02.vocals.wav" {
		t.Fatalf("episode vocals samples = %#v", samples)
	}

	jobNoVocals := *job
	jobNoVocals.VocalsRelPath = ""
	voiceConfig, resolved, source, err = svc.resolveVoiceConfigForSegment(ctx, &jobNoVocals, noProfile)
	if err != nil {
		t.Fatalf("resolve global fallback: %v", err)
	}
	if resolved != nil || source != voiceConfigSourceGlobalDefault || len(voiceConfig) != 0 {
		t.Fatalf("global fallback got profile=%v source=%q config=%#v", resolved, source, voiceConfig)
	}
}

func TestBuildTTSProvenanceMetaRecordsSpokenTextAndVoiceSource(t *testing.T) {
	meta := buildTTSProvenanceMeta(
		"服务器S3被选为leader",
		voiceConfigSourceJobVocals,
		map[string]any{"sample_relpaths": []string{"jobs/7/vocals.wav"}},
	)
	if meta["tts_text_normalized"] != true {
		t.Fatalf("tts_text_normalized = %#v", meta["tts_text_normalized"])
	}
	if meta["tts_spoken_text"] != "三号节点被选为leader" {
		t.Fatalf("tts_spoken_text = %#v", meta["tts_spoken_text"])
	}
	if meta["voice_config_source"] != voiceConfigSourceJobVocals {
		t.Fatalf("voice_config_source = %#v", meta["voice_config_source"])
	}
	if samples := meta["voice_config_sample_relpaths"].([]string); len(samples) != 1 || samples[0] != "jobs/7/vocals.wav" {
		t.Fatalf("voice_config_sample_relpaths = %#v", meta["voice_config_sample_relpaths"])
	}
	rewrites := meta["tts_text_rewrites"].([]pipettstts.SpokenTextRewrite)
	if len(rewrites) != 1 || rewrites[0].Original != "服务器S3" {
		t.Fatalf("tts_text_rewrites = %#v", rewrites)
	}
}

func newTTSTestServer(t *testing.T, captured *capturedTTSRequest) *httptest.Server {
	t.Helper()
	var mu sync.Mutex
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/tts/run" {
			http.NotFound(w, r)
			return
		}
		defer r.Body.Close()
		var req capturedTTSRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			t.Fatalf("decode request: %v", err)
		}
		mu.Lock()
		*captured = req
		mu.Unlock()
		durationMs := int64(req.TargetDurationSec * 1000)
		if durationMs <= 0 {
			durationMs = 1000
		}
		_ = json.NewEncoder(w).Encode(map[string]any{
			"audio_relpath":      req.OutputRelPath,
			"actual_duration_ms": durationMs,
		})
	}))
	t.Cleanup(server.Close)
	return server
}

func newPipelineTestStore(t *testing.T) *store.Store {
	t.Helper()
	name := strings.NewReplacer("/", "_", "\\", "_", " ", "_").Replace(t.Name())
	db, err := gorm.Open(sqlite.Open(fmt.Sprintf("file:%s?mode=memory&cache=shared", name)), &gorm.Config{})
	if err != nil {
		if strings.Contains(err.Error(), "requires cgo") {
			t.Skip("sqlite driver requires cgo in this Windows test environment")
		}
		t.Fatalf("open in-memory sqlite: %v", err)
	}
	st := store.NewWithDB(db)
	if err := st.AutoMigrate(); err != nil {
		t.Fatalf("AutoMigrate: %v", err)
	}
	return st
}

func seedTTSJob(t *testing.T, st *store.Store, vocalsRelPath string, targetText string) (*models.Job, []models.Segment) {
	t.Helper()
	ctx := context.Background()
	job := &models.Job{
		Name:           "tts spoken text test",
		Status:         models.JobStatusRunning,
		CurrentStage:   models.StageTTSDuration,
		SourceLanguage: "en",
		TargetLanguage: "zh",
		VocalsRelPath:  vocalsRelPath,
	}
	if err := st.CreateJob(ctx, job); err != nil {
		t.Fatalf("CreateJob: %v", err)
	}
	if err := st.ReplaceSegments(ctx, job.ID, []models.SegmentDraft{{
		StartMs:      0,
		EndMs:        2000,
		Text:         "server label",
		SpeakerLabel: "SPK_01",
		SplitReason:  "rule",
	}}); err != nil {
		t.Fatalf("ReplaceSegments: %v", err)
	}
	segments, err := st.ListSegments(ctx, job.ID, nil)
	if err != nil {
		t.Fatalf("ListSegments: %v", err)
	}
	segments[0].TargetText = targetText
	segments[0].Status = models.SegmentStatusTranslated
	if err := st.UpdateSegmentTranslations(ctx, []models.Segment{segments[0]}); err != nil {
		t.Fatalf("UpdateSegmentTranslations: %v", err)
	}
	segments, err = st.ListSegments(ctx, job.ID, nil)
	if err != nil {
		t.Fatalf("ListSegments after update: %v", err)
	}
	job, err = st.GetJob(ctx, job.ID)
	if err != nil {
		t.Fatalf("GetJob: %v", err)
	}
	return job, segments
}

func testTTSConfig(dataRoot string) config.Config {
	_ = os.MkdirAll(dataRoot, 0755)
	return config.Config{
		DataRoot:                           dataRoot,
		RetranslationEnabled:               true,
		RetranslationDriftThreshold:        0.06,
		RetranslationMinDriftThreshold:     0.03,
		RetranslationMaxBorrowDriftPct:     0.12,
		RetranslationInitialMaxAttempts:    0,
		RetranslationInitialAbsMaxDriftSec: 0.75,
		VoiceProfileRateAlpha:              0.3,
	}
}

func assertTTSProvenance(t *testing.T, meta map[string]any, wantSpoken string, wantVoiceSource string) {
	t.Helper()
	if meta["tts_text_normalized"] != true {
		t.Fatalf("tts_text_normalized = %#v, want true; meta=%#v", meta["tts_text_normalized"], meta)
	}
	if meta["tts_spoken_text"] != wantSpoken {
		t.Fatalf("tts_spoken_text = %#v, want %q", meta["tts_spoken_text"], wantSpoken)
	}
	if meta["voice_config_source"] != wantVoiceSource {
		t.Fatalf("voice_config_source = %#v, want %q", meta["voice_config_source"], wantVoiceSource)
	}
	rewrites, ok := meta["tts_text_rewrites"].([]any)
	if !ok || len(rewrites) == 0 {
		t.Fatalf("tts_text_rewrites missing: %#v", meta["tts_text_rewrites"])
	}
}
