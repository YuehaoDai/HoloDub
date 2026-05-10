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
      <div class="grid grid-cols-1 md:grid-cols-3 gap-3 mb-3">
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
          <div class="flex items-center justify-between mb-1">
            <div class="text-[10px] uppercase tracking-wider text-[#9db0c9]">输出文件</div>
            <span
              class="text-[9px] px-1.5 py-0.5 rounded-full"
              :class="layoutBadgeClass"
              :title="`output_layout_version=${episode.output_layout_version || 1}`"
            >layout v{{ episode.output_layout_version || 1 }}</span>
          </div>
          <div class="text-xs font-mono text-white truncate" :title="episode.output_relpath || ''">
            {{ episode.output_relpath || "—" }}
          </div>
          <div class="flex items-center gap-2 mt-2">
            <a
              v-if="episode.output_relpath"
              :href="api.episodeFinalUrl(episode.id)"
              target="_blank"
              class="text-[10px] px-2 py-0.5 rounded bg-blue-900/60 text-blue-300 hover:bg-blue-800"
              :download="`episode-${episode.id}-final.mp4`"
            >下载 final.mp4</a>
            <a
              v-if="episode.chapters_manifest_rel_path"
              :href="api.episodeChaptersJsonUrl(episode.id)"
              target="_blank"
              class="text-[10px] px-2 py-0.5 rounded bg-slate-700 text-slate-300 hover:bg-slate-600"
            >chapters.json</a>
            <span
              v-if="hasLoudnormStats"
              class="text-[10px] px-2 py-0.5 rounded bg-emerald-900/40 text-emerald-300"
              title="EBU R128 loudnorm 已应用"
            >loudnorm ✓</span>
          </div>
        </div>
      </div>

      <!-- Episode-level pipeline progress (OPT-402). Each pill is a stage;
           filled pill = stage completed (timestamp present). Order mirrors
           backend EpisodeStageOrder in internal/models/models.go. -->
      <div class="mb-6 p-3 rounded-lg bg-[#1e2535] border border-[#273246]">
        <div class="text-[10px] uppercase tracking-wider text-[#9db0c9] mb-2">Episode 阶段 (OPT-402)</div>
        <div class="flex flex-wrap items-center gap-2">
          <span
            class="text-[10px] px-2 py-0.5 rounded-full"
            :class="episode.vocals_relpath ? 'bg-green-900/60 text-green-300' : 'bg-slate-800 text-slate-500'"
            :title="episode.vocals_relpath || 'separate 阶段未完成'"
          >separate</span>
          <span class="text-[#9db0c9]">›</span>
          <span
            class="text-[10px] px-2 py-0.5 rounded-full"
            :class="episode.asr_done_at ? 'bg-green-900/60 text-green-300' : 'bg-slate-800 text-slate-500'"
            :title="episode.asr_done_at || 'asr_smart 阶段未完成'"
          >asr_smart</span>
          <span class="text-[#9db0c9]">›</span>
          <span
            class="text-[10px] px-2 py-0.5 rounded-full"
            :class="episode.glossary_done_at ? 'bg-green-900/60 text-green-300' : 'bg-slate-800 text-slate-500'"
            :title="episode.glossary_done_at || 'glossary_extract 阶段未完成'"
          >glossary_extract</span>
          <span class="text-[#9db0c9]">›</span>
          <span
            class="text-[10px] px-2 py-0.5 rounded-full"
            :class="hasFannedOut ? 'bg-green-900/60 text-green-300' : 'bg-slate-800 text-slate-500'"
            :title="chapterizePillTitle"
          >chapterize</span>
          <span class="text-[#9db0c9]">›</span>
          <span
            class="text-[10px] px-2 py-0.5 rounded-full"
            :class="episodeMergePillClass"
            :title="episodeMergePillTitle"
          >episode_merge</span>
        </div>
      </div>

      <!-- Reference card (OPT-402) -->
      <div v-if="episode.reference_card" class="mb-6 p-3 rounded-lg bg-[#1e2535] border border-[#273246]">
        <div class="text-[10px] uppercase tracking-wider text-[#9db0c9] mb-1">参考卡 (OPT-402)</div>
        <div class="text-xs text-[#f2f5f7] whitespace-pre-wrap">{{ episode.reference_card }}</div>
      </div>

      <!-- Glossary table (OPT-402). Hidden until ep_glossary_extract finishes
           and writes a non-empty glossary; staying hidden is the desired
           legacy behaviour for old episodes. -->
      <div v-if="episode.glossary && episode.glossary.length" class="mb-6 p-3 rounded-lg bg-[#1e2535] border border-[#273246]">
        <div class="flex items-center justify-between mb-2">
          <div class="text-[10px] uppercase tracking-wider text-[#9db0c9]">
            术语表 (OPT-402, {{ episode.glossary.length }} 项)
          </div>
        </div>
        <table class="w-full text-xs text-left">
          <thead>
            <tr class="text-[#9db0c9] border-b border-[#273246]">
              <th class="font-medium py-1.5 pr-3">Source</th>
              <th class="font-medium py-1.5 pr-3">Target</th>
              <th class="font-medium py-1.5">Note</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="(entry, idx) in episode.glossary"
              :key="`g-${idx}-${entry.source}`"
              class="border-b border-[#273246]/50 hover:bg-[#273246]/30"
            >
              <td class="py-1.5 pr-3 font-mono text-[#f2f5f7]">{{ entry.source }}</td>
              <td class="py-1.5 pr-3 font-mono text-[#f2f5f7]">{{ entry.target }}</td>
              <td class="py-1.5 text-[#9db0c9]">{{ entry.note || "—" }}</td>
            </tr>
          </tbody>
        </table>
      </div>

      <!-- Chapter grid -->
      <div class="mb-3 flex items-center justify-between">
        <h3 class="text-sm font-semibold text-white">Chapters</h3>
        <button class="text-xs text-[#9db0c9] hover:text-white" @click="refresh">↻ 刷新</button>
      </div>
      <div v-if="!chapters.length" class="text-xs text-[#9db0c9] italic">
        暂无章节（OPT-403 chapterize 阶段尚未运行）。
      </div>
      <div v-else class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-3">
        <div
          v-for="ch in chapters"
          :key="ch.id"
          class="p-3 rounded-lg bg-[#1e2535] border border-[#273246] hover:border-blue-500 transition-colors"
        >
          <div class="flex items-center justify-between mb-1">
            <button
              class="text-left flex-1 min-w-0"
              @click="openChapter(ch.id)"
              :title="`#${ch.id} · ${ch.current_stage}`"
            >
              <div class="text-xs font-semibold text-white">
                {{ chapterDisplayTitle(ch) }}
              </div>
              <div
                v-if="chapterSubtitle(ch)"
                class="text-[10px] text-[#9db0c9] truncate mt-0.5"
                :title="ch.chapter_title || ''"
              >{{ chapterSubtitle(ch) }}</div>
            </button>
            <span class="text-[10px] px-1.5 py-0.5 rounded-full ml-2 shrink-0" :class="statusClass(ch.status)">
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
          <div
            v-if="ch.chapter_summary_md"
            class="text-[10px] text-[#9db0c9] mt-2 line-clamp-2"
            :title="ch.chapter_summary_md"
          >{{ ch.chapter_summary_md }}</div>
          <div class="flex items-center gap-2 mt-2 flex-wrap">
            <a
              v-if="ch.output_relpath"
              :href="api.jobFinalUrl(ch.id)"
              target="_blank"
              class="text-[10px] px-1.5 py-0.5 rounded bg-blue-900/60 text-blue-300 hover:bg-blue-800"
              @click.stop
            >下载</a>
            <button
              class="text-[10px] px-1.5 py-0.5 rounded bg-slate-700 text-slate-300 hover:bg-slate-600"
              @click.stop="openChapter(ch.id)"
            >详情</button>
          </div>
          <div v-if="ch.error_message" class="text-[10px] text-red-400 mt-1 truncate" :title="ch.error_message">
            {{ ch.error_message }}
          </div>
        </div>
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

// Chapterize pill goes green once at least one chapter has been fanned
// out (chapter_ordinal >= 2 OR total_chapters > 1). 1-chapter episodes
// short-circuit chapterize; the pill stays grey for them, which is the
// desired UX (operators can tell at a glance whether the DP cut.)
const hasFannedOut = computed(() => {
  if (!episode.value) return false;
  if (episode.value.total_chapters && episode.value.total_chapters > 1) return true;
  return chapters.value.some((c) => (c.chapter_ordinal || 1) > 1);
});

const chapterizePillTitle = computed(() =>
  hasFannedOut.value ? "OPT-403 chapterize 已切分" : "OPT-403 chapterize 未触发或单章节"
);

// episode_merge pill goes green when the unified-layout output is on
// disk (output_layout_version=2 + output_relpath stamped). Yellow when
// in progress (status=merging or chapters all completed but ep status
// not yet completed). Grey otherwise.
const episodeMergePillClass = computed(() => {
  if (!episode.value) return "bg-slate-800 text-slate-500";
  if (
    episode.value.output_layout_version === 2 &&
    episode.value.output_relpath
  ) {
    return "bg-green-900/60 text-green-300";
  }
  if (episode.value.status === "merging") {
    return "bg-amber-900/40 text-amber-300";
  }
  return "bg-slate-800 text-slate-500";
});
const episodeMergePillTitle = computed(() => {
  if (!episode.value) return "OPT-404 episode_merge 未触发";
  if (episode.value.output_layout_version === 2) return "OPT-404 episode_merge 已完成";
  return "OPT-404 episode_merge 未触发或处于旧 layout v1";
});

const layoutBadgeClass = computed(() => {
  const v = episode.value?.output_layout_version || 1;
  return v >= 2
    ? "bg-emerald-900/60 text-emerald-300"
    : "bg-amber-900/40 text-amber-300";
});

const hasLoudnormStats = computed(() => {
  const stats = episode.value?.loudnorm_stats;
  return !!stats && Object.keys(stats).length > 0;
});

// Prefer the LLM-translated title (matches the user's UI language) and
// fall back through source-language title → generic "Chapter N" label.
// Same convention is used by chapters.json so frontend-vs-manifest
// comparisons (back-fill validation) line up.
function chapterDisplayTitle(ch: Job): string {
  const ord = ch.chapter_ordinal || 1;
  if (ch.chapter_title_translated && ch.chapter_title_translated.trim()) {
    return `第 ${ord} 章 · ${ch.chapter_title_translated.trim()}`;
  }
  if (ch.chapter_title && ch.chapter_title.trim()) {
    return `Chapter ${ord} · ${ch.chapter_title.trim()}`;
  }
  return `Chapter ${ord}`;
}

// chapterSubtitle shows the source-language title under the translated
// one when both exist (so editors can verify the translation in the UI
// without having to open the chapter detail). Empty when no source
// title is set (1-chapter back-filled episodes, ep_chapterize disabled).
function chapterSubtitle(ch: Job): string {
  if (
    ch.chapter_title_translated &&
    ch.chapter_title_translated.trim() &&
    ch.chapter_title &&
    ch.chapter_title.trim()
  ) {
    return ch.chapter_title.trim();
  }
  return "";
}

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
