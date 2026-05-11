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
        <div class="p-3 rounded-lg bg-[#1e2535] border border-[#273246] relative">
          <div class="flex items-center justify-between mb-1">
            <div class="text-[10px] uppercase tracking-wider text-[#9db0c9]">Episode 评分 (OPT-406)</div>
            <span
              v-if="episode.episode_judge_meta?.verdict"
              class="text-[9px] px-1.5 py-0.5 rounded-full font-mono"
              :class="episodeJudgeBadgeClass(episode.episode_judge_score ?? 0)"
              :title="episodeJudgeVerdictLabel(episode.episode_judge_meta.verdict)"
            >{{ episodeJudgeVerdictBadge(episode.episode_judge_meta.verdict) }}</span>
          </div>
          <div
            v-if="episode.episode_judge_score !== undefined && episode.episode_judge_score !== null"
            class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-sm font-mono cursor-help group relative"
            :class="episodeJudgeBadgeClass(episode.episode_judge_score)"
            :title="`Episode judge score · ${episodeJudgeVerdictLabel(episode.episode_judge_meta?.verdict)}`"
          >
            <span class="font-semibold">{{ episode.episode_judge_score.toFixed(2) }}</span>
            <!-- Tooltip on hover: 7-axis breakdown + weakest chapters/segments + glossary observed + summary. -->
            <div
              class="hidden group-hover:block absolute z-30 left-0 top-full mt-1 w-80 max-w-[92vw] p-2.5 rounded-lg bg-[#0f1420] border border-[#273246] shadow-xl text-left text-[11px] text-[#cbd6e4] font-sans normal-case"
              @click.stop
            >
              <div class="font-semibold text-white mb-1.5">
                整集 Judge · {{ episodeJudgeVerdictLabel(episode.episode_judge_meta?.verdict) }}
              </div>
              <table class="w-full text-[10px] mb-1.5">
                <tbody>
                  <tr v-for="row in episodeJudgeAxes(episode.episode_judge_meta)" :key="row.label" class="leading-snug">
                    <td class="text-[#9db0c9] pr-1.5">{{ row.label }}</td>
                    <td class="text-right font-mono" :class="episodeJudgeAxisColor(row.value)">{{ row.value !== undefined ? row.value.toFixed(2) : "—" }}</td>
                  </tr>
                </tbody>
              </table>
              <div v-if="episode.episode_judge_meta?.top_3_weakest_chapters && episode.episode_judge_meta.top_3_weakest_chapters.length > 0">
                <div class="font-semibold text-white mt-1.5 mb-1">弱章节 (top {{ episode.episode_judge_meta.top_3_weakest_chapters.length }})</div>
                <ul class="space-y-1 text-[10px]">
                  <li v-for="w in episode.episode_judge_meta.top_3_weakest_chapters" :key="`ch-${w.ordinal}`" class="border-l-2 border-amber-700 pl-1.5">
                    <div class="text-amber-300 font-mono">ch{{ w.ordinal }}</div>
                    <div class="text-[#cbd6e4]">{{ w.issue }}</div>
                    <div class="text-[#9db0c9] italic">→ {{ w.recommended_fix }}</div>
                  </li>
                </ul>
              </div>
              <div v-if="episode.episode_judge_meta?.top_3_weakest_segments && episode.episode_judge_meta.top_3_weakest_segments.length > 0">
                <div class="font-semibold text-white mt-1.5 mb-1">弱段 (top {{ episode.episode_judge_meta.top_3_weakest_segments.length }})</div>
                <ul class="space-y-1 text-[10px]">
                  <li v-for="w in episode.episode_judge_meta.top_3_weakest_segments" :key="`seg-${w.chapter_ordinal}-${w.ordinal}`" class="border-l-2 border-rose-700 pl-1.5">
                    <div class="text-rose-300 font-mono">c{{ w.chapter_ordinal }}.s{{ w.ordinal }}</div>
                    <div class="text-[#cbd6e4]">{{ w.issue }}</div>
                    <div class="text-[#9db0c9] italic">→ {{ w.recommended_fix }}</div>
                  </li>
                </ul>
              </div>
              <div v-if="episode.episode_judge_meta?.terminology_glossary_observed && episode.episode_judge_meta.terminology_glossary_observed.length > 0">
                <div class="font-semibold text-white mt-1.5 mb-1">观测术语表 ({{ episode.episode_judge_meta.terminology_glossary_observed.length }})</div>
                <ul class="space-y-0.5 text-[10px] max-h-32 overflow-y-auto">
                  <li v-for="g in episode.episode_judge_meta.terminology_glossary_observed" :key="g.source_term" class="font-mono">
                    <span class="text-[#cbd6e4]">{{ g.source_term }}</span>
                    <span class="text-[#9db0c9]"> → </span>
                    <span class="text-[#cbd6e4]">{{ g.target_term }}</span>
                    <span v-if="g.note" class="text-amber-300 italic"> · {{ g.note }}</span>
                  </li>
                </ul>
              </div>
              <div
                v-if="episode.episode_judge_meta?.one_paragraph_summary"
                class="mt-1.5 pt-1.5 border-t border-[#273246] text-[10px] text-[#cbd6e4]"
              >{{ episode.episode_judge_meta.one_paragraph_summary }}</div>
            </div>
          </div>
          <div v-else class="text-sm font-mono text-[#9db0c9]">—</div>
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
          <!-- OPT-409 chapter judge score: shown only when CHAPTER_JUDGE_MODEL
               wrote a verdict for this chapter. Hover the badge for the full
               7-axis breakdown + top-3 weakest segments. -->
          <div
            v-if="ch.chapter_judge_score !== undefined && ch.chapter_judge_score !== null"
            class="mt-1.5 inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px] font-mono cursor-help relative group"
            :class="chapterJudgeBadgeClass(ch.chapter_judge_score)"
            :title="`Chapter judge score · ${chapterJudgeVerdictLabel(ch.chapter_judge_meta?.verdict)}`"
          >
            <span class="font-semibold">judge {{ ch.chapter_judge_score.toFixed(2) }}</span>
            <span
              v-if="ch.chapter_judge_meta?.verdict"
              class="text-[9px] opacity-80"
            >· {{ chapterJudgeVerdictBadge(ch.chapter_judge_meta.verdict) }}</span>
            <!-- Tooltip on hover: structured 7-axis breakdown + weakest segments. -->
            <div
              class="hidden group-hover:block absolute z-30 left-0 top-full mt-1 w-72 max-w-[90vw] p-2.5 rounded-lg bg-[#0f1420] border border-[#273246] shadow-xl text-left text-[11px] text-[#cbd6e4] font-sans normal-case"
              @click.stop
            >
              <div class="font-semibold text-white mb-1.5">章节级 Judge · {{ chapterJudgeVerdictLabel(ch.chapter_judge_meta?.verdict) }}</div>
              <table class="w-full text-[10px] mb-1.5">
                <tbody>
                  <tr v-for="row in chapterJudgeAxes(ch.chapter_judge_meta)" :key="row.label" class="leading-snug">
                    <td class="text-[#9db0c9] pr-1.5">{{ row.label }}</td>
                    <td class="text-right font-mono" :class="chapterJudgeAxisColor(row.value)">{{ row.value !== undefined ? row.value.toFixed(2) : "—" }}</td>
                  </tr>
                </tbody>
              </table>
              <div v-if="ch.chapter_judge_meta?.top_3_weakest_segments && ch.chapter_judge_meta.top_3_weakest_segments.length > 0">
                <div class="font-semibold text-white mt-1.5 mb-1">弱段 (top {{ ch.chapter_judge_meta.top_3_weakest_segments.length }})</div>
                <ul class="space-y-1 text-[10px]">
                  <li v-for="w in ch.chapter_judge_meta.top_3_weakest_segments" :key="w.ordinal" class="border-l-2 border-amber-700 pl-1.5">
                    <div class="text-amber-300 font-mono">seg{{ w.ordinal }}</div>
                    <div class="text-[#cbd6e4]">{{ w.issue }}</div>
                    <div class="text-[#9db0c9] italic">→ {{ w.recommended_fix }}</div>
                  </li>
                </ul>
              </div>
              <div
                v-if="ch.chapter_judge_meta?.one_paragraph_summary"
                class="mt-1.5 pt-1.5 border-t border-[#273246] text-[10px] text-[#cbd6e4]"
              >{{ ch.chapter_judge_meta.one_paragraph_summary }}</div>
            </div>
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
import { api, type Episode, type Job, type ChapterJudgeMeta, type EpisodeJudgeMeta } from "../api";

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

// OPT-409 chapter judge UI helpers. Rendering rules:
//   ≥0.85 → green  (chapter ready, glanceable confidence signal)
//   0.7 .. 0.85 → amber (needs revision; review-worthy but not blocking)
//   <0.7 → red    (needs major rework; surfaced visually so editors triage)
function chapterJudgeBadgeClass(score: number): string {
  if (score >= 0.85) return "bg-emerald-900/60 text-emerald-300 border border-emerald-700";
  if (score >= 0.7) return "bg-amber-900/40 text-amber-300 border border-amber-700";
  return "bg-red-900/50 text-red-300 border border-red-700";
}

function chapterJudgeAxisColor(value: number | undefined): string {
  if (value === undefined) return "text-[#9db0c9]";
  if (value >= 0.85) return "text-emerald-300";
  if (value >= 0.7) return "text-amber-300";
  return "text-red-300";
}

function chapterJudgeVerdictBadge(verdict?: string): string {
  switch (verdict) {
    case "chapter_ready": return "ready";
    case "needs_revision": return "revise";
    case "needs_major_rework": return "rework";
    default: return verdict || "";
  }
}

function chapterJudgeVerdictLabel(verdict?: string): string {
  switch (verdict) {
    case "chapter_ready": return "可发布";
    case "needs_revision": return "需修订";
    case "needs_major_rework": return "需重做";
    default: return verdict || "—";
  }
}

// Flatten the structured meta into the table rows shown in the tooltip.
// Order matches what an editor scans top-down: cross-segment axes first
// (the ones segment-level judge cannot see), then aggregate fidelity /
// fluency last for context.
function chapterJudgeAxes(meta: ChapterJudgeMeta | undefined): Array<{ label: string; value: number | undefined }> {
  if (!meta) return [];
  return [
    { label: "叙事连贯", value: meta.narrative_coherence_within_chapter },
    { label: "音色稳定", value: meta.speaker_voice_stability_within_chapter },
    { label: "术语一致", value: meta.terminology_consistency_within_chapter },
    { label: "语域一致", value: meta.register_consistency_within_chapter },
    { label: "整体保真", value: meta.overall_fidelity_chapter },
    { label: "整体流畅", value: meta.overall_fluency_chapter },
  ];
}

// OPT-406 episode judge UI helpers. Episode-level thresholds are stricter
// than chapter-level (0.9 / 0.8 vs 0.85 / 0.7) because the episode judge
// is the final whole-output gate — a borderline-acceptable chapter is
// fine to ship; a borderline-acceptable episode means the episode needs
// rework somewhere the per-chapter judges missed.
function episodeJudgeBadgeClass(score: number): string {
  if (score >= 0.9) return "bg-emerald-900/60 text-emerald-300 border border-emerald-700";
  if (score >= 0.8) return "bg-amber-900/40 text-amber-300 border border-amber-700";
  return "bg-red-900/50 text-red-300 border border-red-700";
}

function episodeJudgeAxisColor(value: number | undefined): string {
  if (value === undefined) return "text-[#9db0c9]";
  if (value >= 0.9) return "text-emerald-300";
  if (value >= 0.8) return "text-amber-300";
  return "text-red-300";
}

function episodeJudgeVerdictBadge(verdict?: string): string {
  switch (verdict) {
    case "production_ready": return "ready";
    case "needs_minor_revision": return "minor";
    case "needs_major_revision": return "major";
    default: return verdict || "";
  }
}

function episodeJudgeVerdictLabel(verdict?: string): string {
  switch (verdict) {
    case "production_ready": return "可发布";
    case "needs_minor_revision": return "需小修";
    case "needs_major_revision": return "需大改";
    default: return verdict || "—";
  }
}

// Flatten the structured meta into the 7-axis table shown in the tooltip.
// Order matches what an editor scans top-down: cross-chapter axes first
// (the ones segment + chapter judges cannot see — terminology / register /
// narrative drift), then character/cultural, then aggregate fidelity /
// fluency last for context.
function episodeJudgeAxes(meta: EpisodeJudgeMeta | undefined): Array<{ label: string; value: number | undefined }> {
  if (!meta) return [];
  return [
    { label: "术语一致 (跨章)", value: meta.terminology_consistency },
    { label: "语域一致 (跨章)", value: meta.register_consistency },
    { label: "叙事连贯 (整集)", value: meta.narrative_coherence },
    { label: "角色音色稳定", value: meta.character_voice_stability },
    { label: "本地化得体", value: meta.cultural_localization },
    { label: "整体保真", value: meta.overall_fidelity },
    { label: "整体流畅", value: meta.overall_fluency },
  ];
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
