<template>
  <div class="p-6">
    <div v-if="loading" class="text-sm text-[#9db0c9]">加载中...</div>
    <div v-else-if="error" class="text-sm text-red-400">{{ error }}</div>
    <template v-else-if="job">
      <!-- Header -->
      <div class="flex items-start justify-between mb-4">
        <div>
          <h2 class="text-lg font-semibold text-white">{{ job.name || `Job #${job.id}` }}</h2>
          <p class="text-xs text-[#9db0c9] mt-1">
            #{{ job.id }} · {{ job.input_relpath }} · {{ job.source_language || "?" }} → {{ job.target_language }}
          </p>
        </div>
        <div class="flex items-center gap-2">
          <span class="text-xs px-2 py-1 rounded-full font-medium" :class="statusClass(job.status)">
            {{ job.status }}
          </span>
          <span class="text-xs px-2 py-1 rounded bg-[#1e2535] text-[#9db0c9]">
            {{ job.current_stage }}
          </span>
        </div>
      </div>

      <!-- 进度与日志（运行中时突出显示） -->
      <div
        v-if="['pending','queued','running'].includes(job.status)"
        class="mb-4 p-3 rounded-lg bg-[#1e2535] border border-[#273246]"
      >
        <div class="flex items-center justify-between mb-2">
          <span class="text-xs font-medium text-[#9db0c9]">任务进度</span>
          <button
            class="text-xs text-blue-400 hover:text-blue-300"
            @click="activeTab = 'stage-runs'"
          >
            查看阶段记录 →
          </button>
        </div>
        <div class="text-xs text-[#f2f5f7]">
          当前阶段：<span class="font-mono">{{ job.current_stage }}</span>
          <span v-if="job.error_message" class="ml-2 text-red-400">· {{ job.error_message }}</span>
        </div>
      </div>

      <!-- 操作按钮 -->
      <div class="flex gap-2 mb-6 flex-wrap">
        <button
          v-if="job.status === 'pending'"
          class="hd-btn hd-btn-primary"
          :disabled="actionLoading"
          @click="startJob"
        >开始</button>
        <button
          v-if="['pending','queued','running'].includes(job.status)"
          class="hd-btn hd-btn-danger"
          :disabled="actionLoading"
          @click="cancelJob"
        >取消任务</button>
        <button
          class="hd-btn hd-btn-secondary"
          :disabled="actionLoading"
          @click="retryMerge"
        >重新合并输出</button>
        <button class="hd-btn hd-btn-ghost" @click="refresh">↻ 刷新</button>
      </div>

      <!-- Tabs -->
      <div class="flex gap-1 mb-4 border-b border-[#1e2535]">
        <button
          v-for="tab in tabs"
          :key="tab.key"
          class="text-xs px-3 py-2 transition-colors"
          :class="activeTab === tab.key
            ? 'border-b-2 border-blue-400 text-white'
            : 'text-[#9db0c9] hover:text-white'"
          @click="activeTab = tab.key"
        >
          {{ tab.label }}
          <span v-if="tab.key === 'segments' && segments.length" class="ml-1 text-[10px] bg-[#1e2535] px-1.5 py-0.5 rounded-full">
            {{ segments.length }}
          </span>
        </button>
      </div>

      <!-- Segments Tab -->
      <div v-if="activeTab === 'segments'">
        <!-- 音色配置面板 -->
        <div v-if="voiceProfiles.length" class="mb-4 rounded-lg border border-[#273246] overflow-hidden text-xs">
          <div class="px-4 py-2 bg-[#1e2535] flex items-center justify-between border-b border-[#273246]">
            <span class="font-medium text-white">音色配置</span>
            <span class="text-[10px] text-[#37465f]">修改后须手动触发合成，不会自动执行</span>
          </div>
          <!-- 步骤 1 -->
          <div class="px-4 py-3 flex items-center gap-3 bg-[#111722]">
            <span class="text-[#9db0c9] shrink-0 w-28">① 选择目标音色</span>
            <select
              v-model="bulkVoiceId"
              class="px-2 py-1 rounded bg-[#1e2535] border border-[#273246] text-[#f2f5f7] flex-1 max-w-xs"
              @wheel.prevent
            >
              <option :value="0">原声（默认，跟随说话人绑定）</option>
              <option v-for="vp in voiceProfiles" :key="vp.id" :value="vp.id">{{ vp.name }}</option>
            </select>
          </div>
          <!-- 步骤 2 -->
          <div class="px-4 py-3 flex items-center gap-3 border-t border-[#273246]">
            <span class="text-[#9db0c9] shrink-0 w-28">② 应用到段落</span>
            <button
              class="px-3 py-1.5 rounded bg-blue-600 hover:bg-blue-500 text-white transition-colors disabled:opacity-50"
              :disabled="bulkApplying"
              @click="applyBulkVoice"
            >
              {{ bulkApplying ? '应用中…' : '为全部段落设置此音色' }}
            </button>
            <span class="text-[#37465f]">仅更新记录，不开始合成，可随时更改</span>
          </div>
          <!-- 步骤 3 -->
          <div class="px-4 py-3 flex items-center gap-3 border-t border-red-900/40 bg-red-950/20">
            <span class="text-[#9db0c9] shrink-0 w-28">③ 开始合成</span>
            <button
              class="px-3 py-1.5 rounded bg-red-700 hover:bg-red-600 text-white transition-colors disabled:opacity-50 font-medium"
              :disabled="resetRetrying"
              @click="resetAndRetryTTS"
            >
              {{ resetRetrying ? '操作中…' : '清除已有音频并重新合成全部' }}
            </button>
            <span class="text-red-400">⚠ 不可撤销 — 所有已生成音频将被清除</span>
          </div>
        </div>
        <SegmentFilter
          v-model:filter="filter"
          v-model:sort="sort"
          v-model:sort-dir="sortDir"
          :high-drift-count="highDriftCount"
        />
        <SegmentTable
          :segments="filteredSegments"
          :job-id="job.id"
          :bindings="bindings"
          :voice-profiles="voiceProfiles"
          @updated="lightRefresh"
          @segment-updated="onSegmentUpdated"
          @binding-updated="onBindingUpdated"
        />
      </div>

      <!-- Stage Runs Tab -->
      <div v-if="activeTab === 'stage-runs'">
        <div class="space-y-2">
          <div
            v-for="run in stageRuns"
            :key="run.id"
            class="bg-[#1e2535] border border-[#273246] rounded-lg px-4 py-3 text-xs"
          >
            <div class="flex items-center justify-between mb-1">
              <span class="font-medium text-white">{{ run.stage }}</span>
              <span :class="stageRunStatusClass(run.status)" class="px-2 py-0.5 rounded-full text-[10px]">
                {{ run.status }}
              </span>
            </div>
            <div class="text-[#9db0c9] space-y-0.5">
              <div>Attempt {{ run.attempt }} · {{ run.duration_ms ? `${(run.duration_ms / 1000).toFixed(1)}s` : '—' }}</div>
              <div v-if="run.error_message" class="text-red-400 mt-1">{{ run.error_message }}</div>
            </div>
          </div>
          <div v-if="!stageRuns.length" class="text-xs text-[#37465f] py-4 text-center">暂无阶段运行记录</div>
        </div>
      </div>

      <!-- Artifacts Tab -->
      <div v-if="activeTab === 'artifacts'">
        <div class="space-y-1.5">
          <div
            v-for="art in artifacts"
            :key="art.relpath"
            class="flex items-center justify-between bg-[#1e2535] rounded-lg px-3 py-2 text-xs"
          >
            <span class="text-[#9db0c9] truncate flex-1 font-mono">{{ art.relpath }}</span>
            <span class="text-[#37465f] shrink-0 ml-3">{{ formatBytes(art.size_bytes) }}</span>
          </div>
          <div v-if="!artifacts.length" class="text-xs text-[#37465f] py-4 text-center">暂无文件</div>
        </div>
      </div>

      <!-- Voice Profiles Tab -->
      <div v-if="activeTab === 'voice-profiles'">
        <VoiceProfileManager />
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from "vue";
import { useRoute } from "vue-router";
import { api, type Job, type Segment, type StageRun, type Artifact, type Binding, type VoiceProfile } from "../api";
import SegmentTable from "./SegmentTable.vue";
import SegmentFilter from "./SegmentFilter.vue";
import VoiceProfileManager from "./VoiceProfileManager.vue";

const route = useRoute();
const jobId = computed(() => Number(route.params.id));

const job = ref<Job | null>(null);
const segments = ref<Segment[]>([]);
const stageRuns = ref<StageRun[]>([]);
const artifacts = ref<Artifact[]>([]);
const bindings = ref<Binding[]>([]);
const voiceProfiles = ref<VoiceProfile[]>([]);
const loading = ref(false);
const error = ref("");
const actionLoading = ref(false);
const bulkVoiceId = ref<number>(0);
const bulkApplying = ref(false);
const resetRetrying = ref(false);
const activeTab = ref("segments");

let currentRefreshAbort: AbortController | null = null;

const tabs = [
  { key: "segments", label: "段落" },
  { key: "stage-runs", label: "阶段记录" },
  { key: "artifacts", label: "输出文件" },
  { key: "voice-profiles", label: "音色管理" },
];

const filter = ref<"all" | "high-drift" | "unsynthesized">("all");
const sort = ref<"ordinal" | "drift">("ordinal");
const sortDir = ref<"asc" | "desc">("asc");

const highDriftCount = computed(() =>
  segments.value.filter((s) => driftPct(s) > 15).length
);

function driftPct(s: Segment): number {
  if (!s.tts_duration_ms || !s.original_duration_ms) return 0;
  return (Math.abs(s.tts_duration_ms - s.original_duration_ms) / s.original_duration_ms) * 100;
}

const filteredSegments = computed(() => {
  let list = [...segments.value];
  if (filter.value === "high-drift") list = list.filter((s) => driftPct(s) > 15);
  if (filter.value === "unsynthesized") list = list.filter((s) => s.status !== "synthesized");
  list.sort((a, b) => {
    const va = sort.value === "drift" ? driftPct(a) : a.ordinal;
    const vb = sort.value === "drift" ? driftPct(b) : b.ordinal;
    return sortDir.value === "asc" ? va - vb : vb - va;
  });
  return list;
});

// Full refresh: fetches all data including static resources.
// Used on mount and after structural changes (retry, cancel, etc.).
async function refresh(silent = false) {
  currentRefreshAbort?.abort();
  const ctrl = new AbortController();
  currentRefreshAbort = ctrl;
  if (!silent) loading.value = true;
  error.value = "";
  try {
    const [j, segs, runs, arts, bnd, vp] = await Promise.all([
      api.getJob(jobId.value, ctrl.signal),
      api.listSegments(jobId.value, ctrl.signal),
      api.listStageRuns(jobId.value, ctrl.signal),
      api.listArtifacts(jobId.value, ctrl.signal),
      api.listBindings(jobId.value, ctrl.signal),
      api.listVoiceProfiles(ctrl.signal),
    ]);
    job.value = j;
    segments.value = segs.segments || [];
    stageRuns.value = runs.stage_runs || [];
    artifacts.value = arts.artifacts || [];
    bindings.value = bnd.bindings || [];
    voiceProfiles.value = vp.voice_profiles || [];
  } catch (e: unknown) {
    if (e instanceof DOMException && e.name === "AbortError") return;
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    if (!silent) loading.value = false;
  }
}

// Light refresh: only job status + segments + stage runs.
// Used for polling and after segment-level operations (quality, rerun).
// Skips artifacts/bindings/voiceProfiles which are expensive or rarely change.
async function lightRefresh() {
  currentRefreshAbort?.abort();
  const ctrl = new AbortController();
  currentRefreshAbort = ctrl;
  try {
    const [j, segs, runs] = await Promise.all([
      api.getJob(jobId.value, ctrl.signal),
      api.listSegments(jobId.value, ctrl.signal),
      api.listStageRuns(jobId.value, ctrl.signal),
    ]);
    job.value = j;
    segments.value = segs.segments || [];
    stageRuns.value = runs.stage_runs || [];
  } catch (e: unknown) {
    if (e instanceof DOMException && e.name === "AbortError") return;
    // Polling failures are silent — don't clobber existing error state.
  }
}

async function startJob() {
  actionLoading.value = true;
  try { await api.startJob(jobId.value); await refresh(); }
  catch (e: unknown) { alert(e instanceof Error ? e.message : String(e)); }
  finally { actionLoading.value = false; }
}

async function cancelJob() {
  actionLoading.value = true;
  try { await api.cancelJob(jobId.value); await refresh(); }
  catch (e: unknown) { alert(e instanceof Error ? e.message : String(e)); }
  finally { actionLoading.value = false; }
}

async function retryMerge() {
  actionLoading.value = true;
  try {
    await api.retryJob(jobId.value, "merge");
    await refresh();
  } catch (e: unknown) {
    alert(e instanceof Error ? e.message : String(e));
  } finally {
    actionLoading.value = false;
  }
}

function statusClass(status: string) {
  switch (status) {
    case "running": return "bg-blue-900/60 text-blue-300";
    case "completed": return "bg-green-900/60 text-green-300";
    case "failed": return "bg-red-900/60 text-red-300";
    case "cancelled": return "bg-slate-700 text-slate-400";
    default: return "bg-slate-800 text-slate-400";
  }
}

function stageRunStatusClass(status: string) {
  switch (status) {
    case "completed": return "bg-green-900/60 text-green-300";
    case "running": return "bg-blue-900/60 text-blue-300";
    case "failed": return "bg-red-900/60 text-red-300";
    default: return "bg-slate-800 text-slate-400";
  }
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

// Handles a single-segment local update (e.g. quality flag) without re-fetching everything.
function onSegmentUpdated(updated: Segment) {
  const idx = segments.value.findIndex((s) => s.id === updated.id);
  if (idx >= 0) {
    segments.value = [
      ...segments.value.slice(0, idx),
      updated,
      ...segments.value.slice(idx + 1),
    ];
  }
}

// Handles a voice binding change locally without re-fetching all bindings.
function onBindingUpdated(speakerLabel: string, voiceProfileId: number) {
  const idx = bindings.value.findIndex((b) => b.speaker?.label === speakerLabel);
  if (idx >= 0) {
    const updated = { ...bindings.value[idx], voice_profile_id: voiceProfileId };
    bindings.value = [
      ...bindings.value.slice(0, idx),
      updated,
      ...bindings.value.slice(idx + 1),
    ];
  }
  // After a binding change the affected segments get re-queued — do a light refresh
  // after a short delay to pick up the new status without hammering the server.
  setTimeout(() => lightRefresh(), 1500);
}

async function applyBulkVoice() {
  if (bulkApplying.value) return;
  const vpName = bulkVoiceId.value === 0
    ? "原声（默认）"
    : (voiceProfiles.value.find((v) => v.id === bulkVoiceId.value)?.name ?? `#${bulkVoiceId.value}`);
  if (!window.confirm(`将把「${vpName}」应用到全部 ${segments.value.length} 个段落。\n\n这只是更新记录，不会开始合成，可随时修改。\n\n确认继续？`)) return;
  bulkApplying.value = true;
  try {
    await api.bulkSetVoice(jobId.value, bulkVoiceId.value);
    await lightRefresh();
  } catch (e: unknown) {
    alert(e instanceof Error ? e.message : String(e));
  } finally {
    bulkApplying.value = false;
  }
}

async function resetAndRetryTTS() {
  if (resetRetrying.value) return;
  const confirmed = window.confirm(
    "⚠ 危险操作，不可撤销\n\n将清除全部已合成音频，并按当前音色设置重新合成。\n\n已合成的音频文件将被清除，无法恢复。\n\n确认继续？"
  );
  if (!confirmed) return;
  resetRetrying.value = true;
  try {
    await api.resetAndRetryTTS(jobId.value);
    await refresh();
  } catch (e: unknown) {
    alert(e instanceof Error ? e.message : String(e));
  } finally {
    resetRetrying.value = false;
  }
}

watch(jobId, () => { currentRefreshAbort?.abort(); refresh(); }, { immediate: false });

// Poll only job status + segments during active runs.
// Use lightRefresh to avoid hammering all 6 endpoints every 5s.
let pollTimer: ReturnType<typeof setInterval> | null = null;
watch(
  () => job.value?.status,
  (status) => {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
    if (status && ["pending", "queued", "running"].includes(status)) {
      pollTimer = setInterval(() => lightRefresh(), 5000);
    }
  },
  { immediate: true }
);
onUnmounted(() => {
  if (pollTimer) clearInterval(pollTimer);
  currentRefreshAbort?.abort();
});

// When filter is "unsynthesized" but all segments are now synthesized, reset filter.
watch(
  () => segments.value,
  (segs) => {
    if (filter.value !== "unsynthesized" || !segs.length) return;
    const unsyn = segs.filter((s) => s.status !== "synthesized");
    if (unsyn.length === 0) filter.value = "all";
  },
  { deep: true }
);

onMounted(() => { refresh(); });
</script>

<style scoped>
.hd-btn {
  @apply text-xs px-3 py-1.5 rounded font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed;
}
.hd-btn-primary {
  @apply bg-blue-600 hover:bg-blue-500 text-white;
}
.hd-btn-danger {
  @apply bg-red-800 hover:bg-red-700 text-white;
}
.hd-btn-secondary {
  @apply bg-[#273246] hover:bg-[#37465f] text-[#f2f5f7];
}
.hd-btn-ghost {
  @apply bg-transparent hover:bg-[#1e2535] text-[#9db0c9] hover:text-white;
}
</style>
