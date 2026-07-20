<template>
  <div class="hd-page">
    <div v-if="loading" class="hd-panel p-5 text-sm text-[color:var(--hd-text-muted)]">
      {{ t("加载中...") }}
    </div>
    <div
      v-else-if="error"
      class="hd-panel border-rose-400/20 bg-rose-500/10 p-5 text-sm text-rose-200"
    >
      {{ error }}
    </div>
    <template v-else-if="job">
      <header class="hd-panel p-4">
        <div class="flex flex-col gap-4 xl:flex-row xl:items-start xl:justify-between">
          <div class="min-w-0">
            <div class="flex flex-wrap items-center gap-2 text-xs text-[color:var(--hd-text-dim)]">
              <span>Jobs</span>
              <span>/</span>
              <span class="font-mono">#{{ job.id }}</span>
              <span>/</span>
              <span class="text-cyan-200">{{ job.current_stage || "idle" }}</span>
            </div>
            <h2 class="mt-2 truncate text-2xl font-semibold text-white">
              {{ job.name || `Job #${job.id}` }}
            </h2>
            <p
              class="mt-2 flex flex-wrap items-center gap-2 text-xs text-[color:var(--hd-text-muted)]"
            >
              <span class="max-w-xl truncate font-mono">{{ job.input_relpath || "—" }}</span>
              <span class="hd-chip hd-chip-muted"
                >{{ job.source_language || "?" }} → {{ job.target_language }}</span
              >
              <router-link
                v-if="job.episode_id"
                :to="`/episodes/${job.episode_id}`"
                class="hd-chip hd-chip-info"
              >
                Episode #{{ job.episode_id }} · Chapter {{ job.chapter_ordinal || 1 }}
              </router-link>
            </p>
          </div>
          <div class="flex flex-wrap items-center gap-2">
            <span class="hd-chip" :class="statusClass(job.status)">{{ job.status }}</span>
            <button
              v-if="job.status === 'pending'"
              class="hd-btn hd-btn-primary"
              :disabled="actionLoading"
              @click="startJob"
            >
              <Play :size="14" />
              {{ t("开始") }}
            </button>
            <button
              v-if="['pending', 'queued', 'running', 'awaiting_review'].includes(job.status)"
              class="hd-btn hd-btn-danger"
              :disabled="actionLoading"
              @click="cancelJob"
            >
              <Square :size="13" />
              {{ t("取消任务") }}
            </button>
            <button class="hd-btn hd-btn-secondary" :disabled="actionLoading" @click="retryMerge">
              <RotateCcw :size="14" />
              {{ t("重新合并输出") }}
            </button>
            <button class="hd-btn hd-btn-ghost" @click="refresh">
              <RefreshCw :size="14" />
              {{ t("刷新") }}
            </button>
          </div>
        </div>

        <div class="mt-4 grid grid-cols-2 gap-3 md:grid-cols-3 2xl:grid-cols-6">
          <div v-for="metric in metricCards" :key="metric.label" class="hd-metric">
            <div class="flex items-center justify-between gap-2">
              <span class="hd-label">{{ metric.label }}</span>
              <component :is="metric.icon" :size="15" :class="metric.iconClass" />
            </div>
            <div class="mt-2 flex items-baseline gap-1">
              <span class="font-mono text-2xl font-semibold" :class="metric.valueClass">{{
                metric.value
              }}</span>
              <span v-if="metric.suffix" class="text-xs text-[color:var(--hd-text-dim)]">{{
                metric.suffix
              }}</span>
            </div>
            <div class="mt-1 text-[10px] text-[color:var(--hd-text-dim)]">{{ metric.caption }}</div>
          </div>
        </div>

        <div class="mt-4 rounded-[8px] border border-[color:var(--hd-border-soft)] bg-black/20 p-3">
          <div class="mb-3 flex items-center justify-between gap-3">
            <div class="hd-label flex items-center gap-1.5">
              <Activity :size="13" />
              {{ t("阶段时间线") }}
            </div>
            <button
              class="text-xs font-semibold text-cyan-200 hover:text-cyan-100"
              @click="activeTab = 'stage-runs'"
            >
              {{ t("查看阶段记录") }}
            </button>
          </div>
          <div class="grid grid-cols-1 gap-2 md:grid-cols-7">
            <div v-for="stage in stageOrder" :key="stage.key" class="relative">
              <div class="flex items-center gap-2">
                <span
                  class="flex h-6 w-6 items-center justify-center rounded-full border text-[10px]"
                  :class="stageDotClass(stage.key)"
                >
                  <component :is="stageIcon(stage.key)" :size="13" />
                </span>
                <div class="min-w-0">
                  <div class="truncate text-xs font-semibold text-white">{{ stage.label }}</div>
                  <div class="mt-0.5 text-[10px] text-[color:var(--hd-text-dim)]">
                    {{ stageCaption(stage.key) }}
                  </div>
                </div>
              </div>
              <div class="mt-2 h-1 rounded-full bg-white/[0.06]">
                <div
                  class="h-full rounded-full"
                  :class="stageBarClass(stage.key)"
                  :style="{ width: stageBarWidth(stage.key) }"
                ></div>
              </div>
            </div>
          </div>
        </div>

        <div
          v-if="job.status === 'awaiting_review'"
          class="mt-4 flex items-start gap-3 rounded-[8px] border border-amber-400/30 bg-amber-500/10 p-3"
        >
          <AlertTriangle :size="17" class="mt-0.5 shrink-0 text-amber-300" />
          <div>
            <div class="text-sm font-semibold text-amber-100">{{ t("等待分段确认") }}</div>
            <p class="mt-1 text-xs leading-5 text-amber-100/80">
              {{
                t(
                  "ASR 分段已完成，AI 已生成合并建议。请在「分段审核」面板中查看建议、手动调整，然后确认分段后继续翻译。",
                )
              }}
            </p>
          </div>
        </div>
      </header>

      <div class="mt-4 grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_320px]">
        <section class="min-w-0">
          <div class="hd-panel overflow-hidden">
            <div
              class="flex flex-wrap gap-1 border-b border-[color:var(--hd-border-soft)] px-3 pt-3"
            >
              <button
                v-for="tab in tabs"
                :key="tab.key"
                class="inline-flex items-center gap-2 border-b-2 px-3 py-2 text-xs font-semibold transition"
                :class="
                  activeTab === tab.key
                    ? 'border-cyan-300 text-white'
                    : 'border-transparent text-[color:var(--hd-text-muted)] hover:text-white'
                "
                @click="activeTab = tab.key"
              >
                {{ tab.label }}
                <span
                  v-if="
                    tab.key === 'segments' && segments.length && job.status !== 'awaiting_review'
                  "
                  class="hd-chip hd-chip-muted py-0.5"
                >
                  {{ segments.length }}
                </span>
                <span v-if="tab.key === 'review'" class="hd-chip hd-chip-warning py-0.5">
                  {{ t("待确认") }}
                </span>
              </button>
            </div>

            <div class="p-3">
              <div v-if="activeTab === 'review'">
                <SegmentReview
                  :job-id="job.id"
                  @confirmed="refresh"
                  @asr-retried="refresh"
                  @segments-changed="lightRefresh"
                />
              </div>

              <div v-if="activeTab === 'segments'">
                <div
                  v-if="voiceProfiles.length"
                  class="mb-4 overflow-hidden rounded-[8px] border border-[color:var(--hd-border-soft)] text-xs"
                >
                  <div
                    class="flex flex-wrap items-center justify-between gap-2 border-b border-[color:var(--hd-border-soft)] bg-white/[0.03] px-4 py-3"
                  >
                    <span class="font-semibold text-white">{{ t("音色配置") }}</span>
                    <span class="text-[10px] text-[color:var(--hd-text-dim)]">{{
                      t("修改后须手动触发合成，不会自动执行")
                    }}</span>
                  </div>
                  <div class="grid gap-0 lg:grid-cols-3">
                    <div
                      class="border-b border-[color:var(--hd-border-soft)] px-4 py-3 lg:border-b-0 lg:border-r"
                    >
                      <div class="hd-label mb-2">{{ t("1 · 选择目标音色") }}</div>
                      <select v-model="bulkVoiceId" class="hd-select" @wheel.prevent>
                        <option :value="0">{{ t("原声（默认，跟随说话人绑定）") }}</option>
                        <option v-for="vp in voiceProfiles" :key="vp.id" :value="vp.id">
                          {{ vp.name }}
                        </option>
                      </select>
                    </div>
                    <div
                      class="border-b border-[color:var(--hd-border-soft)] px-4 py-3 lg:border-b-0 lg:border-r"
                    >
                      <div class="hd-label mb-2">{{ t("2 · 应用到段落") }}</div>
                      <button
                        class="hd-btn hd-btn-secondary w-full"
                        :disabled="bulkApplying"
                        @click="applyBulkVoice"
                      >
                        {{ bulkApplying ? t("应用中…") : t("为全部段落设置此音色") }}
                      </button>
                    </div>
                    <div class="px-4 py-3">
                      <div class="hd-label mb-2 text-rose-200">{{ t("3 · 开始合成") }}</div>
                      <button
                        class="hd-btn hd-btn-danger w-full"
                        :disabled="resetRetrying"
                        @click="resetAndRetryTTS"
                      >
                        {{ resetRetrying ? t("操作中…") : t("清除已有音频并重新合成全部") }}
                      </button>
                    </div>
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

              <div v-if="activeTab === 'stage-runs'" class="hd-table-wrap">
                <table class="hd-table">
                  <thead>
                    <tr>
                      <th>{{ t("阶段") }}</th>
                      <th>{{ t("状态") }}</th>
                      <th>{{ t("尝试次数") }}</th>
                      <th>{{ t("耗时") }}</th>
                      <th>{{ t("错误") }}</th>
                    </tr>
                  </thead>
                  <tbody>
                    <tr v-for="run in stageRuns" :key="run.id">
                      <td class="font-semibold text-white">{{ run.stage }}</td>
                      <td>
                        <span class="hd-chip" :class="stageRunStatusClass(run.status)">{{
                          run.status
                        }}</span>
                      </td>
                      <td class="font-mono text-[color:var(--hd-text-muted)]">{{ run.attempt }}</td>
                      <td class="font-mono text-[color:var(--hd-text-muted)]">
                        {{ run.duration_ms ? `${(run.duration_ms / 1000).toFixed(1)}s` : "—" }}
                      </td>
                      <td class="max-w-md truncate text-rose-200" :title="run.error_message">
                        {{ run.error_message || "—" }}
                      </td>
                    </tr>
                    <tr v-if="!stageRuns.length">
                      <td colspan="5" class="py-8 text-center text-[color:var(--hd-text-dim)]">
                        {{ t("暂无阶段运行记录") }}
                      </td>
                    </tr>
                  </tbody>
                </table>
              </div>

              <div v-if="activeTab === 'artifacts'" class="space-y-2">
                <div
                  v-for="art in artifacts"
                  :key="art.relpath"
                  class="hd-panel-subtle flex items-center justify-between gap-3 px-3 py-2 text-xs"
                >
                  <span
                    class="min-w-0 flex-1 truncate font-mono text-[color:var(--hd-text-muted)]"
                    >{{ art.relpath }}</span
                  >
                  <span class="shrink-0 text-[color:var(--hd-text-dim)]">{{
                    formatBytes(art.size_bytes)
                  }}</span>
                </div>
                <div
                  v-if="!artifacts.length"
                  class="py-8 text-center text-xs text-[color:var(--hd-text-dim)]"
                >
                  {{ t("暂无文件") }}
                </div>
              </div>

              <div v-if="activeTab === 'voice-profiles'">
                <VoiceProfileManager />
              </div>
            </div>
          </div>
        </section>

        <aside class="space-y-4">
          <div class="hd-panel p-4">
            <div class="mb-3 flex items-center justify-between">
              <div class="text-sm font-semibold text-white">{{ t("输出就绪度") }}</div>
              <Archive :size="15" class="text-cyan-200" />
            </div>
            <div class="flex items-center gap-4">
              <div
                class="relative flex h-20 w-20 shrink-0 items-center justify-center rounded-full border border-cyan-300/20 bg-cyan-300/5"
              >
                <span class="font-mono text-xl font-semibold text-cyan-100"
                  >{{ outputReadyPct }}%</span
                >
              </div>
              <div class="min-w-0 flex-1 space-y-2 text-xs">
                <div class="flex justify-between gap-2">
                  <span class="text-[color:var(--hd-text-muted)]">TTS Audio</span
                  ><span class="font-mono text-white"
                    >{{ synthesizedCount }}/{{ segments.length || 0 }}</span
                  >
                </div>
                <div class="flex justify-between gap-2">
                  <span class="text-[color:var(--hd-text-muted)]">{{ t("产物") }}</span
                  ><span class="font-mono text-white">{{ artifacts.length }}</span>
                </div>
                <div class="flex justify-between gap-2">
                  <span class="text-[color:var(--hd-text-muted)]">{{ t("高漂移") }}</span
                  ><span
                    class="font-mono"
                    :class="highDriftCount ? 'text-amber-200' : 'text-emerald-200'"
                    >{{ highDriftCount }}</span
                  >
                </div>
              </div>
            </div>
          </div>

          <div class="hd-panel p-4">
            <div class="mb-3 flex items-center justify-between">
              <div class="text-sm font-semibold text-white">{{ t("高漂移弱段") }}</div>
              <span class="hd-chip hd-chip-danger py-0.5">{{ weakSegments.length }}</span>
            </div>
            <div class="space-y-2">
              <div
                v-for="seg in weakSegments"
                :key="seg.id"
                class="rounded-[7px] border border-[color:var(--hd-border-soft)] bg-white/[0.025] p-2 text-xs"
              >
                <div class="flex items-center justify-between gap-2">
                  <span class="font-mono text-amber-200">#{{ seg.ordinal }}</span>
                  <span class="font-mono text-rose-200">{{ driftPct(seg).toFixed(1) }}%</span>
                </div>
                <div
                  class="mt-1 truncate text-[color:var(--hd-text-muted)]"
                  :title="seg.tgt_text || seg.src_text"
                >
                  {{ seg.tgt_text || seg.src_text || "—" }}
                </div>
              </div>
              <div v-if="!weakSegments.length" class="text-xs text-[color:var(--hd-text-dim)]">
                {{ t("暂无高漂移段落") }}
              </div>
            </div>
          </div>

          <div class="hd-panel p-4">
            <div class="mb-3 flex items-center justify-between">
              <div class="text-sm font-semibold text-white">{{ t("当前错误日志") }}</div>
              <AlertTriangle
                :size="15"
                :class="currentIssue ? 'text-rose-300' : 'text-emerald-300'"
              />
            </div>
            <div
              v-if="currentIssue"
              class="rounded-[7px] border border-rose-400/20 bg-rose-500/10 p-3 text-xs text-rose-100"
            >
              {{ currentIssue }}
            </div>
            <div
              v-else
              class="rounded-[7px] border border-emerald-400/20 bg-emerald-500/10 p-3 text-xs text-emerald-100"
            >
              {{ t("当前没有任务级错误") }}
            </div>
          </div>

          <div class="hd-panel p-4">
            <div class="mb-3 flex items-center justify-between">
              <div class="text-sm font-semibold text-white">{{ t("产物") }}</div>
              <span class="text-xs text-[color:var(--hd-text-dim)]">{{ artifacts.length }}</span>
            </div>
            <div class="space-y-2">
              <div
                v-for="art in artifacts.slice(0, 5)"
                :key="art.relpath"
                class="flex items-center gap-2 text-xs"
              >
                <FileArchive :size="13" class="shrink-0 text-[color:var(--hd-text-dim)]" />
                <span class="min-w-0 flex-1 truncate font-mono text-[color:var(--hd-text-muted)]">{{
                  art.relpath
                }}</span>
                <span class="shrink-0 text-[color:var(--hd-text-dim)]">{{
                  formatBytes(art.size_bytes)
                }}</span>
              </div>
              <div v-if="!artifacts.length" class="text-xs text-[color:var(--hd-text-dim)]">
                {{ t("暂无文件") }}
              </div>
            </div>
          </div>
        </aside>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted } from "vue";
import { useRoute } from "vue-router";
import {
  Activity,
  AlertTriangle,
  Archive,
  Bot,
  Clock3,
  FileArchive,
  Gauge,
  Languages,
  ListChecks,
  Play,
  RefreshCw,
  RotateCcw,
  Scissors,
  Square,
  Waves,
} from "@lucide/vue";
import {
  api,
  type Job,
  type Segment,
  type StageRun,
  type Artifact,
  type Binding,
  type VoiceProfile,
} from "../api";
import { toast } from "../lib/toast";
import { useI18n } from "../i18n";
import SegmentTable from "./SegmentTable.vue";
import SegmentFilter from "./SegmentFilter.vue";
import VoiceProfileManager from "./VoiceProfileManager.vue";
import SegmentReview from "./SegmentReview.vue";

const route = useRoute();
const jobId = computed(() => Number(route.params.id));
const { t } = useI18n();

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

const tabs = computed(() => {
  const base = [
    { key: "segments", label: t("段落") },
    { key: "stage-runs", label: t("阶段记录") },
    { key: "artifacts", label: t("输出文件") },
    { key: "voice-profiles", label: t("音色管理") },
  ];
  if (job.value?.status === "awaiting_review") {
    return [{ key: "review", label: t("分段审核") }, ...base];
  }
  return base;
});

const stageOrder = [
  { key: "media", label: "Media" },
  { key: "separate", label: "Separate" },
  { key: "asr_smart", label: "ASR" },
  { key: "segment_review", label: "Review" },
  { key: "translate", label: "Translate" },
  { key: "tts_duration", label: "TTS" },
  { key: "merge", label: "Merge" },
] as const;

const filter = ref<"all" | "high-drift" | "unsynthesized">("all");
const sort = ref<"ordinal" | "drift">("ordinal");
const sortDir = ref<"asc" | "desc">("asc");

const highDriftCount = computed(() => segments.value.filter((s) => driftPct(s) > 15).length);

const synthesizedCount = computed(
  () => segments.value.filter((s) => s.status === "synthesized").length,
);

const outputReadyPct = computed(() => {
  if (segments.value.length) {
    return Math.round((synthesizedCount.value / segments.value.length) * 100);
  }
  return job.value?.status === "completed" ? 100 : 0;
});

const totalStageDurationMs = computed(() =>
  stageRuns.value.reduce((sum, run) => sum + (run.duration_ms || 0), 0),
);

const averageJudgeScore = computed(() => {
  const scores = segments.value
    .map((s) => s.judge_score)
    .filter((score): score is number => score !== undefined && score !== null);
  if (scores.length) {
    return Math.round((scores.reduce((sum, s) => sum + s, 0) / scores.length) * 100);
  }
  if (job.value?.chapter_judge_score !== undefined && job.value.chapter_judge_score !== null) {
    return Math.round(job.value.chapter_judge_score * 100);
  }
  return null;
});

const weakSegments = computed(() =>
  [...segments.value]
    .filter((s) => driftPct(s) > 15)
    .sort((a, b) => driftPct(b) - driftPct(a))
    .slice(0, 5),
);

const currentIssue = computed(() => {
  if (job.value?.error_message) return job.value.error_message;
  const failed = stageRuns.value.find((run) => run.status === "failed" && run.error_message);
  return failed?.error_message || "";
});

const metricCards = computed(() => [
  {
    label: t("当前阶段"),
    value: job.value?.current_stage || "idle",
    suffix: "",
    caption: job.value?.status || t("未开始"),
    icon: Activity,
    iconClass: "text-cyan-200",
    valueClass: "text-cyan-100 text-lg",
  },
  {
    label: t("段落数"),
    value: segments.value.length ? String(segments.value.length) : "—",
    suffix: "",
    caption: `${synthesizedCount.value} ${t("已合成")}`,
    icon: ListChecks,
    iconClass: "text-emerald-200",
    valueClass: "text-white",
  },
  {
    label: t("高漂移"),
    value: String(highDriftCount.value),
    suffix: ">15%",
    caption: t("需要时间轴复查"),
    icon: AlertTriangle,
    iconClass: highDriftCount.value ? "text-amber-200" : "text-emerald-200",
    valueClass: highDriftCount.value ? "text-amber-200" : "text-emerald-200",
  },
  {
    label: t("输出就绪"),
    value: String(outputReadyPct.value),
    suffix: "%",
    caption: `${artifacts.value.length} ${t("产物")}`,
    icon: Archive,
    iconClass: "text-cyan-200",
    valueClass: "text-cyan-100",
  },
  {
    label: t("AI 评分"),
    value: averageJudgeScore.value === null ? "—" : String(averageJudgeScore.value),
    suffix: averageJudgeScore.value === null ? "" : "/100",
    caption: averageJudgeScore.value === null ? t("未启用评审") : t("平均评审分"),
    icon: Bot,
    iconClass: "text-violet-200",
    valueClass: "text-violet-200",
  },
  {
    label: t("运行时长"),
    value: totalStageDurationMs.value ? formatDurationShort(totalStageDurationMs.value) : "—",
    suffix: "",
    caption: t("已记录阶段耗时"),
    icon: Clock3,
    iconClass: "text-[color:var(--hd-text-muted)]",
    valueClass: "text-white text-lg",
  },
]);

function driftPct(s: Segment): number {
  if (!s.tts_duration_ms || !s.original_duration_ms) return 0;
  return (Math.abs(s.tts_duration_ms - s.original_duration_ms) / s.original_duration_ms) * 100;
}

function formatDurationShort(ms: number): string {
  const totalSec = Math.round(ms / 1000);
  if (totalSec < 60) return `${totalSec}s`;
  const minutes = Math.floor(totalSec / 60);
  const seconds = totalSec % 60;
  if (minutes < 60) return `${minutes}m ${seconds}s`;
  const hours = Math.floor(minutes / 60);
  return `${hours}h ${minutes % 60}m`;
}

function stageRunFor(stage: string): StageRun | undefined {
  return stageRuns.value.find((run) => run.stage === stage);
}

function stageState(stage: string): "completed" | "active" | "failed" | "pending" {
  const run = stageRunFor(stage);
  if (run?.status === "failed") return "failed";
  if (run?.status === "completed") return "completed";
  if (run?.status === "running" || job.value?.current_stage === stage) return "active";
  const currentIndex = stageOrder.findIndex((s) => s.key === job.value?.current_stage);
  const stageIndex = stageOrder.findIndex((s) => s.key === stage);
  if (currentIndex >= 0 && stageIndex >= 0 && stageIndex < currentIndex) return "completed";
  return "pending";
}

function stageIcon(stage: string) {
  switch (stage) {
    case "media":
      return FileArchive;
    case "separate":
      return Waves;
    case "asr_smart":
      return Languages;
    case "segment_review":
      return Scissors;
    case "translate":
      return Languages;
    case "tts_duration":
      return Gauge;
    case "merge":
      return Archive;
    default:
      return Activity;
  }
}

function stageCaption(stage: string): string {
  const run = stageRunFor(stage);
  if (run?.duration_ms) return formatDurationShort(run.duration_ms);
  const state = stageState(stage);
  if (state === "active") return t("正在运行");
  if (state === "completed") return t("已完成");
  if (state === "failed") return t("失败");
  return t("等待中");
}

function stageDotClass(stage: string): string {
  switch (stageState(stage)) {
    case "completed":
      return "border-emerald-400/40 bg-emerald-400/12 text-emerald-200";
    case "active":
      return "border-cyan-300/50 bg-cyan-300/15 text-cyan-100";
    case "failed":
      return "border-rose-300/45 bg-rose-400/12 text-rose-200";
    default:
      return "border-white/10 bg-white/[0.03] text-[color:var(--hd-text-dim)]";
  }
}

function stageBarClass(stage: string): string {
  switch (stageState(stage)) {
    case "completed":
      return "bg-emerald-400";
    case "active":
      return "bg-cyan-300";
    case "failed":
      return "bg-rose-400";
    default:
      return "bg-white/10";
  }
}

function stageBarWidth(stage: string): string {
  switch (stageState(stage)) {
    case "completed":
      return "100%";
    case "active":
      return "58%";
    case "failed":
      return "100%";
    default:
      return "18%";
  }
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
  try {
    await api.startJob(jobId.value);
    await refresh();
    toast.success(t("任务已开始"));
  } catch (e: unknown) {
    toast.fromError(e, t("开始任务失败"));
  } finally {
    actionLoading.value = false;
  }
}

async function cancelJob() {
  actionLoading.value = true;
  try {
    await api.cancelJob(jobId.value);
    await refresh();
    toast.info(t("已请求取消，等待 Worker 中断当前阶段"));
  } catch (e: unknown) {
    toast.fromError(e, t("取消任务失败"));
  } finally {
    actionLoading.value = false;
  }
}

async function retryMerge() {
  actionLoading.value = true;
  try {
    await api.retryJob(jobId.value, "merge");
    await refresh();
    toast.success(t("已重新触发 merge 阶段"));
  } catch (e: unknown) {
    toast.fromError(e, t("merge 重试失败"));
  } finally {
    actionLoading.value = false;
  }
}

function statusClass(status: string) {
  switch (status) {
    case "running":
      return "hd-chip-info";
    case "awaiting_review":
      return "hd-chip-warning";
    case "completed":
      return "hd-chip-success";
    case "failed":
      return "hd-chip-danger";
    case "cancelled":
      return "hd-chip-muted";
    default:
      return "hd-chip-muted";
  }
}

function stageRunStatusClass(status: string) {
  switch (status) {
    case "completed":
      return "hd-chip-success";
    case "running":
      return "hd-chip-info";
    case "failed":
      return "hd-chip-danger";
    default:
      return "hd-chip-muted";
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
    segments.value = [...segments.value.slice(0, idx), updated, ...segments.value.slice(idx + 1)];
  }
}

// Handles a voice binding change locally without re-fetching all bindings.
function onBindingUpdated(speakerLabel: string, voiceProfileId: number) {
  const idx = bindings.value.findIndex((b) => b.speaker?.label === speakerLabel);
  if (idx >= 0) {
    const updated = { ...bindings.value[idx], voice_profile_id: voiceProfileId };
    bindings.value = [...bindings.value.slice(0, idx), updated, ...bindings.value.slice(idx + 1)];
  }
  // After a binding change the affected segments get re-queued — do a light refresh
  // after a short delay to pick up the new status without hammering the server.
  setTimeout(() => lightRefresh(), 1500);
}

async function applyBulkVoice() {
  if (bulkApplying.value) return;
  const vpName =
    bulkVoiceId.value === 0
      ? t("原声（默认）")
      : (voiceProfiles.value.find((v) => v.id === bulkVoiceId.value)?.name ??
        `#${bulkVoiceId.value}`);
  if (
    !window.confirm(
      t(
        "将把「{name}」应用到全部 {count} 个段落。\n\n这只是更新记录，不会开始合成，可随时修改。\n\n确认继续？",
        {
          name: vpName,
          count: segments.value.length,
        },
      ),
    )
  )
    return;
  bulkApplying.value = true;
  try {
    await api.bulkSetVoice(jobId.value, bulkVoiceId.value);
    await lightRefresh();
    toast.success(t("已批量应用音色"));
  } catch (e: unknown) {
    toast.fromError(e, t("批量应用音色失败"));
  } finally {
    bulkApplying.value = false;
  }
}

async function resetAndRetryTTS() {
  if (resetRetrying.value) return;
  const confirmed = window.confirm(
    t(
      "危险操作，不可撤销\n\n将清除全部已合成音频，并按当前音色设置重新合成。\n\n已合成的音频文件将被清除，无法恢复。\n\n确认继续？",
    ),
  );
  if (!confirmed) return;
  resetRetrying.value = true;
  try {
    await api.resetAndRetryTTS(jobId.value);
    await refresh();
    toast.warning(t("已清除合成结果并重新排队"));
  } catch (e: unknown) {
    toast.fromError(e, t("重置 TTS 失败"));
  } finally {
    resetRetrying.value = false;
  }
}

watch(
  jobId,
  () => {
    currentRefreshAbort?.abort();
    refresh();
  },
  { immediate: false },
);

// Poll only job status + segments during active runs.
// Use lightRefresh to avoid hammering all 6 endpoints every 5s.
let pollTimer: ReturnType<typeof setInterval> | null = null;
watch(
  () => job.value?.status,
  (status, prevStatus) => {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
    if (status && ["pending", "queued", "running", "awaiting_review"].includes(status)) {
      pollTimer = setInterval(() => lightRefresh(), 5000);
    }
    // Auto-switch to review tab when job enters awaiting_review
    if (status === "awaiting_review" && prevStatus !== "awaiting_review") {
      activeTab.value = "review";
    }
    // Leave review tab when job is no longer in awaiting_review
    if (prevStatus === "awaiting_review" && status !== "awaiting_review") {
      activeTab.value = "segments";
    }
  },
  { immediate: true },
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
  { deep: true },
);

onMounted(() => {
  refresh();
});
</script>
