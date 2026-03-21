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

function apiHeaders(): Record<string, string> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  const key = getApiKey();
  if (key) headers["X-API-Key"] = key;
  return headers;
}

async function apiFetch<T = unknown>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(path, {
    ...options,
    headers: { ...apiHeaders(), ...(options.headers as Record<string, string> || {}) },
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error((payload as { message?: string; error?: string }).message || (payload as { error?: string }).error || `Request failed: ${response.status}`);
  }
  return payload as T;
}

export interface Job {
  id: number;
  name: string;
  status: string;
  current_stage: string;
  input_relpath: string;
  source_language: string;
  target_language: string;
  error_message?: string;
  created_at: string;
  updated_at: string;
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
  meta?: Record<string, unknown>;
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

export interface VoiceProfile {
  id: number;
  name: string;
  mode: string;
  provider: string;
  language: string;
}

export interface Binding {
  id: number;
  speaker_id: number;
  voice_profile_id: number;
  speaker?: { id: number; label: string };
  voice_profile?: VoiceProfile;
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
  }) => apiFetch<Job>("/jobs", { method: "POST", body: JSON.stringify(data) }),

  getJob: (id: number) => apiFetch<Job>(`/jobs/${id}`),

  startJob: (id: number) => apiFetch(`/jobs/${id}/start`, { method: "POST" }),

  cancelJob: (id: number) => apiFetch(`/jobs/${id}/cancel`, { method: "POST" }),

  retryJob: (id: number, stage?: string, segmentIds?: number[]) =>
    apiFetch(`/jobs/${id}/retry`, {
      method: "POST",
      body: JSON.stringify({ stage, segment_ids: segmentIds }),
    }),

  listSegments: (id: number) => apiFetch<{ segments: Segment[] }>(`/jobs/${id}/segments`),

  listStageRuns: (id: number) => apiFetch<{ stage_runs: StageRun[] }>(`/jobs/${id}/stage-runs`),

  listArtifacts: (id: number) => apiFetch<{ artifacts: Artifact[] }>(`/jobs/${id}/artifacts`),

  patchSegment: (jobId: number, segmentId: number, targetText: string, rerun: boolean) =>
    apiFetch(`/jobs/${jobId}/segments/${segmentId}`, {
      method: "PATCH",
      body: JSON.stringify({ target_text: targetText, rerun }),
    }),

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

  originalAudioUrl: (jobId: number, ordinal: number): string => {
    const key = getApiKey();
    const base = `/jobs/${jobId}/audio/${ordinal}`;
    return key ? `${base}?api_key=${encodeURIComponent(key)}` : base;
  },

  listBindings: (jobId: number) =>
    apiFetch<{ bindings: Binding[] }>(`/jobs/${jobId}/bindings`),

  listVoiceProfiles: () =>
    apiFetch<{ voice_profiles: VoiceProfile[] }>("/voice-profiles"),

  upsertBindings: (jobId: number, bindings: { speaker_label: string; voice_profile_id: number }[], rerunAffected?: boolean) =>
    apiFetch(`/jobs/${jobId}/bindings`, {
      method: "PUT",
      body: JSON.stringify({
        bindings: bindings.map((b) => ({ speaker_label: b.speaker_label, voice_profile_id: b.voice_profile_id })),
        rerun_affected: !!rerunAffected,
      }),
    }),

  mlHealth: () =>
    apiFetch<{ tts_warmup_status?: string; adapters?: Record<string, string> }>("/ml-health"),
};
