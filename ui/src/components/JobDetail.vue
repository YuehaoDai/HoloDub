<template>
  <div class="p-6">
    <div v-if="loading" class="text-sm text-[#9db0c9]">加载中...</div>
    <div v-else-if="error" class="text-sm text-red-400">{{ error }}</div>
    <template v-else-if="job">
      <!-- Header -->
      <div class="flex items-start justify-between mb-6">
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

      <!-- 操作按钮 -->
      <div class="flex gap-2 mb-6 flex-wrap">
        <button
          v-if="job.status === 'pending'"
          class="hd-btn hd-btn-primary"
          :disabled="actionLoading"
          @click="startJob"
        >开始</button>
        <button
          v-if="['pending','running'].includes(job.status)"
          class="hd-btn hd-btn-danger"
          :disabled="actionLoading"
          @click="cancelJob"
        >取消</button>
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
        <SegmentFilter
          v-model:filter="filter"
          v-model:sort="sort"
          v-model:sort-dir="sortDir"
          :high-drift-count="highDriftCount"
        />
        <SegmentTable
          :segments="filteredSegments"
          :job-id="job.id"
          @updated="refresh"
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
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted } from "vue";
import { useRoute } from "vue-router";
import { api, type Job, type Segment, type StageRun, type Artifact } from "../api";
import SegmentTable from "./SegmentTable.vue";
import SegmentFilter from "./SegmentFilter.vue";

const route = useRoute();
const jobId = computed(() => Number(route.params.id));

const job = ref<Job | null>(null);
const segments = ref<Segment[]>([]);
const stageRuns = ref<StageRun[]>([]);
const artifacts = ref<Artifact[]>([]);
const loading = ref(false);
const error = ref("");
const actionLoading = ref(false);
const activeTab = ref("segments");

const tabs = [
  { key: "segments", label: "段落" },
  { key: "stage-runs", label: "阶段记录" },
  { key: "artifacts", label: "输出文件" },
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

async function refresh() {
  loading.value = true;
  error.value = "";
  try {
    const [j, segs, runs, arts] = await Promise.all([
      api.getJob(jobId.value),
      api.listSegments(jobId.value),
      api.listStageRuns(jobId.value),
      api.listArtifacts(jobId.value),
    ]);
    job.value = j;
    segments.value = segs.segments || [];
    stageRuns.value = runs.stage_runs || [];
    artifacts.value = arts.artifacts || [];
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    loading.value = false;
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

watch(jobId, () => { refresh(); }, { immediate: false });

// 当筛选为「未合成」但所有段落已合成时，自动切回「全部」，避免列表突然变空
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
