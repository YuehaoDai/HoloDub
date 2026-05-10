<template>
  <div class="p-6">
    <div v-if="loading" class="text-sm text-[#9db0c9]">加载中...</div>
    <div v-else-if="error" class="text-sm text-red-400">{{ error }}</div>
    <template v-else-if="episode">
      <!-- Header -->
      <div class="flex items-start justify-between mb-4">
        <div>
          <h2 class="text-lg font-semibold text-white">
            {{ episode.name || `Episode #${episode.id}` }}
          </h2>
          <p class="text-xs text-[#9db0c9] mt-1">
            #{{ episode.id }} · {{ episode.source_video_relpath || "—" }} ·
            {{ episode.source_language || "?" }} → {{ episode.target_language || "?" }}
          </p>
        </div>
        <div class="flex items-center gap-2">
          <span class="text-xs px-2 py-1 rounded-full font-medium" :class="statusClass(episode.status)">
            {{ episode.status }}
          </span>
          <span class="text-xs px-2 py-1 rounded bg-[#1e2535] text-[#9db0c9]">
            {{ completedChapters }}/{{ episode.total_chapters }} chapters
          </span>
        </div>
      </div>

      <!-- Episode-level metadata cards -->
      <div class="grid grid-cols-1 md:grid-cols-3 gap-3 mb-6">
        <div class="p-3 rounded-lg bg-[#1e2535] border border-[#273246]">
          <div class="text-[10px] uppercase tracking-wider text-[#9db0c9] mb-1">总时长</div>
          <div class="text-sm font-mono text-white">
            {{ episode.duration_ms ? formatDuration(episode.duration_ms) : "—" }}
          </div>
        </div>
        <div class="p-3 rounded-lg bg-[#1e2535] border border-[#273246]">
          <div class="text-[10px] uppercase tracking-wider text-[#9db0c9] mb-1">Episode 评分 (OPT-406)</div>
          <div class="text-sm font-mono text-white">
            {{ episode.episode_judge_score !== undefined && episode.episode_judge_score !== null
                ? episode.episode_judge_score.toFixed(2)
                : "—" }}
          </div>
        </div>
        <div class="p-3 rounded-lg bg-[#1e2535] border border-[#273246]">
          <div class="text-[10px] uppercase tracking-wider text-[#9db0c9] mb-1">输出文件</div>
          <div class="text-xs font-mono text-white truncate">
            {{ episode.output_relpath || "—" }}
          </div>
        </div>
      </div>

      <!-- Reference card placeholder (OPT-402) -->
      <div v-if="episode.reference_card" class="mb-6 p-3 rounded-lg bg-[#1e2535] border border-[#273246]">
        <div class="text-[10px] uppercase tracking-wider text-[#9db0c9] mb-1">参考卡 (OPT-402)</div>
        <div class="text-xs text-[#f2f5f7] whitespace-pre-wrap">{{ episode.reference_card }}</div>
      </div>

      <!-- Chapter grid -->
      <div class="mb-3 flex items-center justify-between">
        <h3 class="text-sm font-semibold text-white">Chapters</h3>
        <button class="text-xs text-[#9db0c9] hover:text-white" @click="refresh">↻ 刷新</button>
      </div>
      <div v-if="!chapters.length" class="text-xs text-[#9db0c9] italic">
        暂无章节（OPT-403 chapterize 阶段尚未运行）。
      </div>
      <div v-else class="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-3">
        <button
          v-for="ch in chapters"
          :key="ch.id"
          class="p-3 rounded-lg bg-[#1e2535] border border-[#273246] hover:border-blue-500 text-left transition-colors"
          @click="openChapter(ch.id)"
        >
          <div class="flex items-center justify-between mb-1">
            <span class="text-xs font-semibold text-white">
              Chapter {{ ch.chapter_ordinal || 1 }}
            </span>
            <span class="text-[10px] px-1.5 py-0.5 rounded-full" :class="statusClass(ch.status)">
              {{ ch.status }}
            </span>
          </div>
          <div class="text-[10px] text-[#9db0c9] truncate">
            #{{ ch.id }} · {{ ch.current_stage }}
          </div>
          <div
            v-if="ch.chapter_start_ms !== undefined && ch.chapter_end_ms"
            class="text-[10px] text-[#9db0c9] mt-1 font-mono"
          >
            {{ formatDuration(ch.chapter_start_ms) }} – {{ formatDuration(ch.chapter_end_ms) }}
          </div>
          <div v-if="ch.error_message" class="text-[10px] text-red-400 mt-1 truncate">
            {{ ch.error_message }}
          </div>
        </button>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { api, type Episode, type Job } from "../api";

const route = useRoute();
const router = useRouter();

const episode = ref<Episode | null>(null);
const chapters = ref<Job[]>([]);
const loading = ref(false);
const error = ref("");

const episodeId = computed(() => Number(route.params.id));

const completedChapters = computed(() =>
  chapters.value.filter((c) => c.status === "completed").length
);

function statusClass(status: string) {
  switch (status) {
    case "running":
    case "chaptering":
    case "dispatched":
    case "merging":
    case "judging":
    case "reworking":
      return "bg-blue-900/60 text-blue-300";
    case "completed": return "bg-green-900/60 text-green-300";
    case "failed":
    case "timed_out":
      return "bg-red-900/60 text-red-300";
    case "cancelled": return "bg-slate-700 text-slate-400";
    default: return "bg-slate-800 text-slate-400";
  }
}

function formatDuration(ms: number): string {
  const totalSec = Math.floor(ms / 1000);
  const h = Math.floor(totalSec / 3600);
  const m = Math.floor((totalSec % 3600) / 60);
  const s = totalSec % 60;
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${m}:${String(s).padStart(2, "0")}`;
}

function openChapter(jobId: number) {
  router.push(`/jobs/${jobId}`);
}

async function refresh() {
  if (!episodeId.value) return;
  loading.value = true;
  error.value = "";
  try {
    const ep = await api.getEpisode(episodeId.value);
    episode.value = ep;
    // Episode response already preloads Chapters but we re-query the list
    // endpoint to keep the component honest if the preload is ever dropped
    // for performance (and as a parity check during back-fill validation).
    if (ep.chapters && ep.chapters.length) {
      chapters.value = ep.chapters;
    } else {
      const data = await api.getEpisodeChapters(episodeId.value);
      chapters.value = data.chapters || [];
    }
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    loading.value = false;
  }
}

onMounted(refresh);
watch(episodeId, refresh);
</script>
