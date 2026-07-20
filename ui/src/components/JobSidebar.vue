<template>
  <nav class="flex min-h-0 flex-1 flex-col">
    <div class="flex-shrink-0 border-b border-[color:var(--hd-border-soft)] p-3">
      <div
        class="grid grid-cols-2 gap-1 rounded-[8px] border border-[color:var(--hd-border-soft)] bg-black/20 p-1"
      >
        <!-- Tab switcher (OPT-401: Episodes vs Jobs) -->
        <button
          class="flex items-center justify-center gap-1.5 rounded-[6px] px-2 py-2 text-xs font-semibold transition"
          :class="
            activeTab === 'jobs'
              ? 'bg-[color:var(--hd-surface-3)] text-white shadow-sm'
              : 'text-[color:var(--hd-text-muted)] hover:text-white'
          "
          @click="setActiveTab('jobs')"
        >
          <Workflow :size="13" />
          {{ t("任务") }}
        </button>
        <button
          class="flex items-center justify-center gap-1.5 rounded-[6px] px-2 py-2 text-xs font-semibold transition"
          :class="
            activeTab === 'episodes'
              ? 'bg-[color:var(--hd-surface-3)] text-white shadow-sm'
              : 'text-[color:var(--hd-text-muted)] hover:text-white'
          "
          @click="setActiveTab('episodes')"
        >
          <PanelsTopLeft :size="13" />
          {{ t("剧集") }}
        </button>
      </div>

      <div class="mt-3 grid grid-cols-3 gap-2 text-[10px]">
        <div class="hd-panel-subtle px-2 py-2">
          <div class="text-[color:var(--hd-text-dim)]">{{ t("运行中") }}</div>
          <div class="mt-1 font-mono text-sm text-cyan-200">{{ summary.running }}</div>
        </div>
        <div class="hd-panel-subtle px-2 py-2">
          <div class="text-[color:var(--hd-text-dim)]">{{ t("已就绪") }}</div>
          <div class="mt-1 font-mono text-sm text-emerald-200">{{ summary.completed }}</div>
        </div>
        <div class="hd-panel-subtle px-2 py-2">
          <div class="text-[color:var(--hd-text-dim)]">{{ t("问题") }}</div>
          <div class="mt-1 font-mono text-sm text-rose-200">{{ summary.failed }}</div>
        </div>
      </div>
    </div>

    <div class="min-h-0 flex-1 overflow-y-auto py-3">
      <div class="px-3 pb-2">
        <div class="hd-label">
          {{ activeTab === "jobs" ? t("最近任务") : t("最近剧集") }}
        </div>
      </div>
      <div v-if="loading" class="space-y-2 px-3">
        <div
          v-for="i in 6"
          :key="i"
          class="h-[58px] animate-pulse rounded-[8px] bg-white/[0.045]"
        ></div>
      </div>
      <div
        v-else-if="error"
        class="mx-3 rounded-[8px] border border-amber-400/20 bg-amber-500/10 p-3 text-xs text-amber-100"
      >
        <div class="flex items-center gap-2 font-semibold">
          <TriangleAlert :size="14" />
          {{ t("API 未连接") }}
        </div>
        <p class="mt-1.5 leading-relaxed text-amber-100/80">{{ friendlyError }}</p>
      </div>

      <template v-else-if="activeTab === 'jobs'">
        <button
          v-for="job in sortedJobs"
          :key="job.id"
          class="group mx-3 mb-2 w-[calc(100%-1.5rem)] rounded-[8px] border p-3 text-left transition"
          :class="itemClass(selectedJobId === job.id)"
          @click="$emit('select', job.id)"
        >
          <div class="flex items-start justify-between gap-2">
            <div class="min-w-0">
              <div class="truncate text-xs font-semibold text-white">
                {{ job.name || `Job #${job.id}` }}
              </div>
              <div
                class="mt-1 flex items-center gap-1.5 truncate text-[10px] text-[color:var(--hd-text-dim)]"
              >
                <span>#{{ job.id }}</span>
                <span>·</span>
                <span class="truncate">{{ job.current_stage || "idle" }}</span>
              </div>
            </div>
            <span class="hd-chip shrink-0" :class="statusClass(job.status)">
              {{ job.status }}
            </span>
          </div>
          <div class="mt-3 h-1 rounded-full bg-white/[0.06]">
            <div
              class="h-full rounded-full"
              :class="progressClass(job.status)"
              :style="{ width: progressWidth(job.status) }"
            ></div>
          </div>
        </button>
        <div
          v-if="!sortedJobs.length"
          class="px-4 py-8 text-center text-xs text-[color:var(--hd-text-dim)]"
        >
          {{ t("暂无任务") }}
        </div>
      </template>

      <template v-else>
        <button
          v-for="ep in sortedEpisodes"
          :key="ep.id"
          class="group mx-3 mb-2 w-[calc(100%-1.5rem)] rounded-[8px] border p-3 text-left transition"
          :class="itemClass(selectedEpisodeId === ep.id)"
          @click="selectEpisode(ep.id)"
        >
          <div class="flex items-start justify-between gap-2">
            <div class="min-w-0">
              <div class="truncate text-xs font-semibold text-white">
                {{ ep.name || `Episode #${ep.id}` }}
              </div>
              <div
                class="mt-1 flex items-center gap-1.5 truncate text-[10px] text-[color:var(--hd-text-dim)]"
              >
                <span>#{{ ep.id }}</span>
                <span>·</span>
                <span>{{ ep.total_chapters }} chapter{{ ep.total_chapters > 1 ? "s" : "" }}</span>
              </div>
            </div>
            <span class="hd-chip shrink-0" :class="statusClass(ep.status)">
              {{ ep.status }}
            </span>
          </div>
        </button>
        <div
          v-if="!sortedEpisodes.length"
          class="px-4 py-8 text-center text-xs text-[color:var(--hd-text-dim)]"
        >
          {{ t("暂无 Episode") }}
        </div>
      </template>
    </div>

    <div class="flex-shrink-0 border-t border-[color:var(--hd-border-soft)] p-3">
      <button class="hd-btn hd-btn-ghost w-full justify-start" @click="load(false)">
        <RefreshCw :size="14" />
        {{ t("刷新列表") }}
      </button>
    </div>
  </nav>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from "vue";
import { useRouter, useRoute } from "vue-router";
import { PanelsTopLeft, RefreshCw, TriangleAlert, Workflow } from "@lucide/vue";
import { api, type Job, type Episode } from "../api";
import { useI18n } from "../i18n";

defineProps<{ selectedJobId: number | null }>();
defineEmits<{ select: [id: number] }>();

type Tab = "jobs" | "episodes";

const route = useRoute();
const router = useRouter();
const { t } = useI18n();
const jobs = ref<Job[]>([]);
const episodes = ref<Episode[]>([]);
const loading = ref(false);
const error = ref("");
const activeTab = ref<Tab>(deriveInitialTab());
let timer: ReturnType<typeof setInterval> | null = null;

const sortedJobs = computed(() => [...jobs.value].sort((a, b) => b.id - a.id));

const sortedEpisodes = computed(() => [...episodes.value].sort((a, b) => b.id - a.id));

const selectedEpisodeId = computed(() => {
  if (!route.path.startsWith("/episodes")) return null;
  const id = Number(route.params.id);
  return Number.isFinite(id) ? id : null;
});

const currentItems = computed(() => (activeTab.value === "jobs" ? jobs.value : episodes.value));

const summary = computed(() => {
  const items = currentItems.value;
  return {
    running: items.filter((x) => isActiveStatus(x.status)).length,
    completed: items.filter((x) => x.status === "completed").length,
    failed: items.filter((x) => ["failed", "timed_out"].includes(x.status)).length,
  };
});

const friendlyError = computed(() => {
  if (!error.value) return "";
  if (error.value.includes("public base URL") || error.value.includes("/ui/jobs")) {
    return t(
      "当前只连到了 Vite 前端预览，后端 API 尚未代理到 /jobs。UI 仍可预览，连接 Go API 后会显示真实任务。",
    );
  }
  return error.value.length > 160 ? `${error.value.slice(0, 160)}...` : error.value;
});

function deriveInitialTab(): Tab {
  // Honour the URL on first paint so a deep link to /episodes/:id keeps the
  // sidebar tab in sync without a flash of the wrong list.
  return route.path.startsWith("/episodes") ? "episodes" : "jobs";
}

function setActiveTab(tab: Tab) {
  if (activeTab.value === tab) return;
  activeTab.value = tab;
  load(false);
}

function selectEpisode(id: number) {
  router.push(`/episodes/${id}`);
}

function statusClass(status: string) {
  switch (status) {
    case "running":
    case "chaptering":
    case "dispatched":
    case "merging":
    case "judging":
    case "reworking":
      return "hd-chip-info";
    case "completed":
      return "hd-chip-success";
    case "failed":
    case "timed_out":
      return "hd-chip-danger";
    case "awaiting_review":
      return "hd-chip-warning";
    case "cancelled":
      return "hd-chip-muted";
    default:
      return "hd-chip-muted";
  }
}

function itemClass(selected: boolean) {
  return selected
    ? "border-cyan-300/35 bg-cyan-300/10 shadow-[inset_3px_0_0_rgba(32,213,238,0.9)]"
    : "border-[color:var(--hd-border-soft)] bg-white/[0.025] hover:border-cyan-300/25 hover:bg-white/[0.05]";
}

function isActiveStatus(status: string): boolean {
  return [
    "pending",
    "queued",
    "running",
    "awaiting_review",
    "chaptering",
    "dispatched",
    "merging",
    "judging",
    "reworking",
  ].includes(status);
}

function progressWidth(status: string): string {
  switch (status) {
    case "completed":
      return "100%";
    case "failed":
    case "timed_out":
      return "100%";
    case "running":
    case "merging":
    case "judging":
    case "reworking":
      return "62%";
    case "awaiting_review":
      return "48%";
    case "queued":
      return "22%";
    default:
      return "12%";
  }
}

function progressClass(status: string): string {
  if (status === "completed") return "bg-emerald-400";
  if (["failed", "timed_out"].includes(status)) return "bg-rose-400";
  if (status === "awaiting_review") return "bg-amber-400";
  return "bg-cyan-300";
}

async function load(silent = false) {
  if (!silent) loading.value = true;
  error.value = "";
  try {
    if (activeTab.value === "jobs") {
      const data = await api.listJobs();
      jobs.value = data.jobs || [];
    } else {
      const data = await api.listEpisodes();
      episodes.value = data.episodes || [];
    }
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    if (!silent) loading.value = false;
  }
}

watch(
  () => route.path,
  (path) => {
    if (path.startsWith("/episodes") && activeTab.value !== "episodes") {
      activeTab.value = "episodes";
      load(false);
    }
  },
);

onMounted(() => {
  load();
  timer = setInterval(() => load(true), 30000);
});

onUnmounted(() => {
  if (timer) clearInterval(timer);
});
</script>
