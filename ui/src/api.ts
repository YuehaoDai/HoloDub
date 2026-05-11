// API key state lives here so it is the single source of truth for both
// the typed JSON client (lib/api-client.ts) and the legacy callers in
// components that still build URLs by hand (audio playback URLs).
let _apiKey = "";

export function setApiKey(key: string) {
  _apiKey = key;
  localStorage.setItem("hd_api_key", key);
}

export function getApiKey(): string {
  if (!_apiKey) {
    _apiKey = localStorage.getItem("hd_api_key") || "";
  }
  return _apiKey;
}

import { httpJson, ApiError } from "./lib/api-client";
export { ApiError };

// Thin shim that preserves the historical `apiFetch<T>(path, init)` shape so
// each call site does not need to be rewritten in this PR. New code should
// import httpJson directly from ./lib/api-client.
async function apiFetch<T = unknown>(path: string, options: RequestInit = {}): Promise<T> {
  return httpJson<T>(path, options as Parameters<typeof httpJson>[1]);
}

export interface Job {
  id: number;
  name: string;
  status: string;
  current_stage: string;
  input_relpath: string;
  source_language: string;
  target_language: string;
  output_relpath?: string;
  error_message?: string;
  created_at: string;
  updated_at: string;
  // OPT-401 Episode/Chapter columns. Older API bundles will simply not return
  // these fields and the UI degrades gracefully (no episode link, ordinal=1).
  episode_id?: number;
  chapter_ordinal?: number;
  chapter_start_ms?: number;
  chapter_end_ms?: number;
  // OPT-403 chapter metadata. Empty for the historical 1-chapter Episodes
  // (back-fill leaves them blank); populated by chapter_review LLM after
  // fan-out for OPT-403 Episodes. EpisodeDetail prefers chapter_title_
  // translated when rendering, falls back to chapter_title, then to a
  // generic "Chapter N" / "第 N 章" label.
  chapter_title?: string;
  chapter_title_translated?: string;
  chapter_summary_md?: string;
  // OPT-409 chapter-level judge. Both undefined when CHAPTER_JUDGE_MODEL
  // is empty (judging disabled) or when the async chapter judge call has
  // not yet completed for this chapter (legacy 1-chapter Jobs back-filled
  // by OPT-401 also stay undefined — the judge fires only at runMerge,
  // which never re-ran on those rows).
  chapter_judge_score?: number; // 0..1, currently equal to ChapterJudgeResult.overall_fidelity_chapter
  chapter_judge_meta?: ChapterJudgeMeta;
}

// ChapterJudgeMeta is the structured chapter-level judge verdict written
// by the OPT-409 hook in pipeline.runMerge. Mirrors the Go schema in
// internal/llm/chapter_judge.go (ChapterJudgeResult).
export interface ChapterJudgeMeta {
  narrative_coherence_within_chapter?: number;
  speaker_voice_stability_within_chapter?: number;
  terminology_consistency_within_chapter?: number;
  register_consistency_within_chapter?: number;
  overall_fidelity_chapter?: number;
  overall_fluency_chapter?: number;
  top_3_weakest_segments?: Array<{
    ordinal: number;
    issue: string;
    recommended_fix: string;
  }>;
  verdict?: "chapter_ready" | "needs_revision" | "needs_major_rework";
  one_paragraph_summary?: string;
}

// EpisodeJudgeMeta is the structured episode-level judge verdict written
// by the OPT-406 hook in pipeline.runEpisodeMerge. Mirrors the Go schema
// in internal/llm/episode_judge.go (EpisodeJudgeResult). Seven axes vs
// chapter judge's six (adds character_voice_stability +
// cultural_localization), and TWO weakest arrays (whole chapters + pin-
// pointed segments) so OPT-407 closed-loop rework can dispatch chapter-
// level OR segment-level retranslation.
export interface EpisodeJudgeMeta {
  terminology_consistency?: number;
  register_consistency?: number;
  narrative_coherence?: number;
  character_voice_stability?: number;
  cultural_localization?: number;
  overall_fidelity?: number;
  overall_fluency?: number;
  top_3_weakest_chapters?: Array<{
    ordinal: number;
    issue: string;
    recommended_fix: string;
  }>;
  top_3_weakest_segments?: Array<{
    chapter_ordinal: number;
    ordinal: number;
    issue: string;
    recommended_fix: string;
  }>;
  terminology_glossary_observed?: Array<{
    source_term: string;
    target_term: string;
    note?: string;
  }>;
  verdict?: "production_ready" | "needs_minor_revision" | "needs_major_revision";
  one_paragraph_summary?: string;
}

// GlossaryEntry is one (source -> target) translation pair the canonical
// episode glossary published by the OPT-402 ep_glossary_extract stage.
// Older API bundles return no glossary field — UI degrades gracefully.
export interface GlossaryEntry {
  source: string;
  target: string;
  note?: string;
}

// Episode is the long-form container introduced by OPT-401. A single Episode
// owns 1..N chapter Jobs (each Job is a chapter). For historical 1-chapter
// videos the back-fill ensures Episode.id == Job.id.
//
// OPT-402 added vocals/bgm relpaths (mirrored from the chapter that wrote
// them), a `glossary` jsonb array, and two progress timestamps
// (asr_done_at, glossary_done_at) that drive the EpisodeDetail
// "episode-level stages" tracker.
export interface Episode {
  id: number;
  tenant_key?: string;
  name: string;
  source_video_relpath: string;
  source_language: string;
  target_language: string;
  duration_ms: number;
  total_chapters: number;
  status: string;
  output_relpath?: string;
  error_message?: string;
  reference_card?: string;
  // OPT-406 episode-level judge — populated by maybeJudgeEpisodeAsync after
  // ep_episode_merge transitions the episode to Completed. Score is the
  // scalar 0..1 (currently equal to EpisodeJudgeResult.overall_fidelity);
  // meta carries the full 7-axis verdict + weakest-chapters + weakest-
  // segments + cross-chapter glossary observations + summary.
  episode_judge_score?: number;
  episode_judge_meta?: EpisodeJudgeMeta;
  completed_at?: string | null;
  created_at: string;
  updated_at: string;
  chapters?: Job[];
  // OPT-402 episode-level pipeline fields. All optional so the UI can
  // render a partial Episode while the pipeline is still running.
  vocals_relpath?: string;
  bgm_relpath?: string;
  asr_done_at?: string | null;
  glossary_done_at?: string | null;
  glossary?: GlossaryEntry[];
  // OPT-403 unified output layout fields.
  // - output_layout_version: 1 = legacy jobs/{id}/output/... (back-fill
  //   pending); 2 = unified episodes/{id}/... (UI uses /episodes/{id}/
  //   download/final and chapters.json links).
  // - chapters_manifest_relpath: path to chapters.json relative to
  //   DATA_ROOT. Empty until ep_episode_merge writes it.
  // - loudnorm_stats: opaque map (vp{N}_master, vp{N}_chXX) — UI only
  //   needs to detect non-empty for the "audio normalised" badge.
  output_layout_version?: number;
  chapters_manifest_rel_path?: string;
  loudnorm_stats?: Record<string, unknown>;
}

export interface Segment {
  id: number;
  job_id: number;
  ordinal: number;
  start_ms: number;
  end_ms: number;
  original_duration_ms: number;
  src_text: string;
  tgt_text: string;
  tts_audio_path: string;
  tts_duration_ms: number;
  status: string;
  speaker_label: string;
  voice_profile_id?: number;
  meta?: Record<string, unknown>;
  // OPT-002 LLM-as-Judge MVP. Both undefined when judging is disabled
  // (JUDGE_MODEL=""), or when the async judge call has not yet completed.
  judge_score?: number; // 0..1, currently equal to JudgeResult.fidelity
  judge_meta?: {
    fidelity?: number;
    fluency?: number;
    coherence?: number;
    issues?: string[];
    verdict?: "accept" | "retry" | "split";
  };
}

export interface StageRun {
  id: number;
  stage: string;
  status: string;
  attempt: number;
  error_message: string;
  duration_ms: number;
  started_at: string;
  finished_at: string | null;
}

export interface Artifact {
  relpath: string;
  size_bytes: number;
  modified_at: string;
}

export interface FileEntry {
  name: string;
  is_dir: boolean;
  relpath: string;
  size_bytes?: number;
  modified_at?: string;
}

export interface VoiceProfile {
  id: number;
  name: string;
  mode: string;
  provider: string;
  language: string;
  sample_relpaths?: string[];
  checkpoint_relpath?: string;
  validation_status?: string;
  validation_error?: string;
}

export interface Binding {
  id: number;
  speaker_id: number;
  voice_profile_id: number;
  speaker?: { id: number; label: string };
  voice_profile?: VoiceProfile;
}

export interface SegmentSuggestion {
  id: number;
  job_id: number;
  ordinal: number;
  action: "merge" | "split";
  segment_ids: number[];
  split_char_index: number;
  reason: string;
  confidence: number;
  status: "pending" | "accepted" | "rejected";
  created_at: string;
}

export const api = {
  listJobs: () => apiFetch<{ jobs: Job[] }>("/jobs"),

  createJob: (data: {
    name?: string;
    input_relpath: string;
    source_language?: string;
    target_language: string;
    max_retries?: number;
    auto_start?: boolean;
    episode_id?: number;
  }) => apiFetch<Job>("/jobs", { method: "POST", body: JSON.stringify(data) }),

  getJob: (id: number, signal?: AbortSignal) => apiFetch<Job>(`/jobs/${id}`, { signal }),

  // OPT-401 Episode endpoints.
  listEpisodes: (signal?: AbortSignal) =>
    apiFetch<{ episodes: Episode[] }>("/episodes", { signal }),

  getEpisode: (id: number, signal?: AbortSignal) =>
    apiFetch<Episode>(`/episodes/${id}`, { signal }),

  getEpisodeChapters: (id: number, signal?: AbortSignal) =>
    apiFetch<{ chapters: Job[] }>(`/episodes/${id}/chapters`, { signal }),

  startJob: (id: number) => apiFetch(`/jobs/${id}/start`, { method: "POST" }),

  cancelJob: (id: number) => apiFetch(`/jobs/${id}/cancel`, { method: "POST" }),

  retryJob: (id: number, stage?: string, segmentIds?: number[]) =>
    apiFetch(`/jobs/${id}/retry`, {
      method: "POST",
      body: JSON.stringify({ stage, segment_ids: segmentIds }),
    }),

  listSegments: (id: number, signal?: AbortSignal) => apiFetch<{ segments: Segment[] }>(`/jobs/${id}/segments`, { signal }),

  listStageRuns: (id: number, signal?: AbortSignal) => apiFetch<{ stage_runs: StageRun[] }>(`/jobs/${id}/stage-runs`, { signal }),

  listArtifacts: (id: number, signal?: AbortSignal) => apiFetch<{ artifacts: Artifact[] }>(`/jobs/${id}/artifacts`, { signal }),

  patchSegment: (jobId: number, segmentId: number, targetText: string, rerun: boolean, voiceProfileId?: number | null) =>
    apiFetch(`/jobs/${jobId}/segments/${segmentId}`, {
      method: "PATCH",
      body: JSON.stringify({
        target_text: targetText,
        rerun,
        ...(voiceProfileId !== undefined ? { voice_profile_id: voiceProfileId ?? 0 } : {}),
      }),
    }),

  patchSegmentTimes: (jobId: number, segmentId: number, startMs: number, endMs: number) =>
    apiFetch(`/jobs/${jobId}/segments/${segmentId}`, {
      method: "PATCH",
      body: JSON.stringify({ start_ms: startMs, end_ms: endMs }),
    }),

  // patchSegmentSrcText updates the ASR transcript of a single segment
  // during the awaiting_review stage.  The backend rejects it with 409 if
  // the parent job is not in awaiting_review, and 400 if the trimmed text
  // is empty or larger than 8 KiB.  It only writes source_text — timing,
  // status, target_text and tts_* fields are untouched.
  patchSegmentSrcText: (jobId: number, segmentId: number, srcText: string) =>
    apiFetch<{ updated: boolean; segment_id: number; src_text: string }>(
      `/jobs/${jobId}/segments/${segmentId}`,
      {
        method: "PATCH",
        body: JSON.stringify({ src_text: srcText }),
      }
    ),

  // retrySegmentASR clips the segment's time window out of the job's
  // vocals (or input) audio and re-runs faster-whisper, then writes the
  // result back into source_text.  Only allowed in awaiting_review.
  // Response shapes (200 in both cases):
  //   { updated: true,  segment_id, src_text }
  //   { updated: false, segment_id, warning: "empty_transcription", message }
  // Failures map to 409 / 404 / 502 / 500 with the standard error envelope.
  retrySegmentASR: (jobId: number, segmentId: number) =>
    apiFetch<{
      updated: boolean
      segment_id: number
      src_text?: string
      warning?: string
      message?: string
    }>(`/jobs/${jobId}/segments/${segmentId}/retry-asr`, { method: "POST" }),

  rerunSegment: (jobId: number, segmentId: number) =>
    apiFetch(`/jobs/${jobId}/segments/${segmentId}/rerun`, { method: "POST" }),

  patchSegmentQuality: (jobId: number, segmentId: number, quality: "good" | "bad" | "skip") =>
    apiFetch(`/jobs/${jobId}/segments/${segmentId}/quality`, {
      method: "PATCH",
      body: JSON.stringify({ quality }),
    }),

  segmentAudioUrl: (jobId: number, ordinal: number): string => {
    const key = getApiKey();
    const base = `/jobs/${jobId}/tts/${ordinal}`;
    return key ? `${base}?api_key=${encodeURIComponent(key)}` : base;
  },

  // OPT-403/404 download helpers. Construct browser-friendly URLs so
  // <a download> / <video src> work without a custom fetch wrapper.
  // The api_key is appended as a query param to match the
  // segmentAudioUrl convention (router.go accepts both the
  // X-API-Key header and the api_key query param).
  episodeFinalUrl: (episodeId: number): string => {
    const key = getApiKey();
    const base = `/episodes/${episodeId}/download/final`;
    return key ? `${base}?api_key=${encodeURIComponent(key)}` : base;
  },
  episodeChaptersJsonUrl: (episodeId: number): string => {
    const key = getApiKey();
    const base = `/episodes/${episodeId}/chapters.json`;
    return key ? `${base}?api_key=${encodeURIComponent(key)}` : base;
  },
  jobFinalUrl: (jobId: number): string => {
    const key = getApiKey();
    const base = `/jobs/${jobId}/download/final`;
    return key ? `${base}?api_key=${encodeURIComponent(key)}` : base;
  },

  originalAudioUrl: (jobId: number, ordinal: number, v?: number, previewStartMs?: number, previewEndMs?: number): string => {
    const key = getApiKey();
    const base = `/jobs/${jobId}/audio/${ordinal}`;
    const params = new URLSearchParams();
    // URLSearchParams.set already encodes values — do NOT wrap with encodeURIComponent
    if (key) params.set('api_key', key);
    // v combines both start and end so any time range change produces a distinct URL
    if (v !== undefined) params.set('v', String(v));
    if (previewStartMs !== undefined) params.set('start_ms', String(previewStartMs));
    if (previewEndMs !== undefined) params.set('end_ms', String(previewEndMs));
    const q = params.toString();
    return q ? `${base}?${q}` : base;
  },

  listBindings: (jobId: number, signal?: AbortSignal) =>
    apiFetch<{ bindings: Binding[] }>(`/jobs/${jobId}/bindings`, { signal }),

  listVoiceProfiles: (signal?: AbortSignal) =>
    apiFetch<{ voice_profiles: VoiceProfile[] }>("/voice-profiles", { signal }),

  listFiles: (dir?: string, filter?: "video" | "audio" | "all", signal?: AbortSignal) => {
    const params = new URLSearchParams();
    if (dir) params.set("dir", dir);
    if (filter && filter !== "all") params.set("filter", filter);
    return apiFetch<{ dir: string; entries: FileEntry[] }>(`/files?${params}`, { signal });
  },

  createVoiceProfile: (data: {
    name: string;
    mode?: string;
    provider?: string;
    language?: string;
    sample_relpaths?: string[];
    checkpoint_relpath?: string;
    index_relpath?: string;
    config_relpath?: string;
    internal_speaker_id?: string;
    meta?: Record<string, unknown>;
  }) =>
    apiFetch<VoiceProfile>("/voice-profiles", {
      method: "POST",
      body: JSON.stringify(data),
    }),

  validateVoiceProfile: (id: number) =>
    apiFetch<{ voice_profile_id: number; status: string; missing_paths: string[] }>(
      `/voice-profiles/${id}/validate`,
      { method: "POST" }
    ),

  updateVoiceProfile: (id: number, data: {
    name: string;
    mode?: string;
    provider?: string;
    language?: string;
    sample_relpaths?: string[];
    checkpoint_relpath?: string;
    index_relpath?: string;
    config_relpath?: string;
  }) =>
    apiFetch<VoiceProfile>(`/voice-profiles/${id}`, {
      method: "PATCH",
      body: JSON.stringify(data),
    }),

  deleteVoiceProfile: (id: number) =>
    apiFetch<void>(`/voice-profiles/${id}`, { method: "DELETE" }),

  previewVoice: (jobId: number, segmentId: number, voiceProfileId: number) =>
    apiFetch<{ audio_relpath: string; actual_duration_ms: number; preview_url: string }>(
      `/jobs/${jobId}/segments/${segmentId}/preview-voice`,
      { method: "POST", body: JSON.stringify({ voice_profile_id: voiceProfileId }) }
    ),

  previewVoiceUrl: (jobId: number, segmentId: number, voiceProfileId: number): string => {
    const key = getApiKey();
    let url = `/jobs/${jobId}/preview-voice/${segmentId}?vp=${voiceProfileId}`;
    if (key) url += `&api_key=${encodeURIComponent(key)}`;
    return url;
  },

  upsertBindings: (jobId: number, bindings: { speaker_label: string; voice_profile_id: number }[], rerunAffected?: boolean) =>
    apiFetch(`/jobs/${jobId}/bindings`, {
      method: "PUT",
      body: JSON.stringify({
        bindings: bindings.map((b) => ({ speaker_label: b.speaker_label, voice_profile_id: b.voice_profile_id })),
        rerun_affected: !!rerunAffected,
      }),
    }),

  bulkSetVoice: (jobId: number, voiceProfileId: number) =>
    apiFetch(`/jobs/${jobId}/segments/voice`, {
      method: "PUT",
      body: JSON.stringify({ voice_profile_id: voiceProfileId }),
    }),

  resetAndRetryTTS: (jobId: number) =>
    apiFetch(`/jobs/${jobId}/segments/reset-tts`, { method: "POST" }),

  mlHealth: () =>
    apiFetch<{ tts_warmup_status?: string; adapters?: Record<string, string> }>("/ml-health"),

  // ── Segment review ────────────────────────────────────────────────────────
  listSegmentSuggestions: (jobId: number, signal?: AbortSignal) =>
    apiFetch<{ suggestions: SegmentSuggestion[] }>(`/jobs/${jobId}/segment-suggestions`, { signal }),

  acceptSuggestion: (jobId: number, suggestionId: number) =>
    apiFetch(`/jobs/${jobId}/segment-suggestions/${suggestionId}/accept`, { method: "POST" }),

  rejectSuggestion: (jobId: number, suggestionId: number) =>
    apiFetch(`/jobs/${jobId}/segment-suggestions/${suggestionId}/reject`, { method: "POST" }),

  mergeSegments: (jobId: number, segmentIds: number[]) =>
    apiFetch(`/jobs/${jobId}/segments/merge`, {
      method: "POST",
      body: JSON.stringify({ segment_ids: segmentIds }),
    }),

  splitSegment: (jobId: number, segmentId: number, splitCharIndex: number) =>
    apiFetch(`/jobs/${jobId}/segments/${segmentId}/split`, {
      method: "POST",
      body: JSON.stringify({ split_char_index: splitCharIndex }),
    }),

  confirmSegmentation: (jobId: number) =>
    apiFetch(`/jobs/${jobId}/confirm-segmentation`, { method: "POST" }),

  retryASR: (jobId: number) =>
    apiFetch(`/jobs/${jobId}/retry-asr`, { method: "POST" }),
};
