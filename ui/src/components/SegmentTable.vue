<template>
  <div ref="tableContainerRef" tabindex="0" class="outline-none" @keydown="onKeydown">
    <!-- 批量操作栏 -->
    <div
      v-if="selectedIds.size > 0"
      class="mb-3 flex items-center gap-3 rounded-[8px] border border-cyan-300/25 bg-cyan-300/10 px-3 py-2 text-xs"
    >
      <span class="font-semibold text-cyan-100">{{
        t("已选 {count} 段", { count: selectedIds.size })
      }}</span>
      <button class="hd-btn hd-btn-primary py-1.5" :disabled="batchRerunning" @click="batchRerun">
        {{ batchRerunning ? t("提交中…") : t("批量重合成") }}
      </button>
      <button class="hd-btn hd-btn-ghost py-1.5" @click="clearSelection">
        {{ t("取消选择") }}
      </button>
    </div>
    <div class="hd-table-wrap overflow-x-auto">
      <table class="hd-table min-w-[1160px]">
        <thead>
          <tr>
            <th class="px-2 py-2 text-center w-8">
              <input
                type="checkbox"
                :checked="allSelected"
                :indeterminate="someSelected"
                class="rounded border-[color:var(--hd-border)] bg-[color:var(--hd-surface)]"
                @change="toggleSelectAll"
              />
            </th>
            <th class="px-3 py-2 text-left font-medium w-10">#</th>
            <th class="px-3 py-2 text-left font-medium">{{ t("原文") }}</th>
            <th class="px-3 py-2 text-left font-medium">{{ t("译文") }}</th>
            <th class="px-3 py-2 text-center font-medium w-24">{{ t("声音") }}</th>
            <th class="px-3 py-2 text-center font-medium w-20">{{ t("漂移率") }}</th>
            <th class="px-3 py-2 text-center font-medium w-20">{{ t("偏差时长") }}</th>
            <!-- OPT-002: AI 评分 (observe-only). Hidden when no segments have judge_score. -->
            <th
              v-if="hasAnyJudgeScore"
              class="px-3 py-2 text-center font-medium w-16"
              :title="t('OPT-002 LLM-as-Judge: 0-100, 综合保真/流畅/连贯')"
            >
              {{ t("AI 评分") }}
            </th>
            <th class="px-3 py-2 text-center font-medium w-40">{{ t("原声 / TTS") }}</th>
            <th class="px-3 py-2 text-center font-medium w-16">{{ t("操作") }}</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="(seg, index) in segments" :key="seg.id">
            <tr
              class="transition-colors"
              :class="{
                'bg-cyan-300/[0.055]': editingId === seg.id,
                'bg-cyan-300/[0.04]': selectedIds.has(seg.id),
                'ring-1 ring-blue-500/50': focusedIndex === index,
              }"
              :ref="(el) => setRowRef(seg.ordinal, el)"
              @click="focusRow(seg, $event)"
            >
              <td class="px-2 py-2 text-center">
                <input
                  v-if="seg.status === 'synthesized'"
                  type="checkbox"
                  :checked="selectedIds.has(seg.id)"
                  class="rounded border-[color:var(--hd-border)] bg-[color:var(--hd-surface)]"
                  @change="toggleSelect(seg)"
                />
                <span v-else class="text-[color:var(--hd-text-dim)]">—</span>
              </td>
              <td class="px-3 py-2 text-[color:var(--hd-text-muted)] font-mono">
                {{ seg.ordinal }}
              </td>
              <td class="px-3 py-2 text-[color:var(--hd-text-muted)] max-w-xs">
                <div class="truncate" :title="seg.src_text">{{ seg.src_text }}</div>
              </td>
              <td class="px-3 py-2 text-white max-w-xs">
                <div class="truncate" :title="seg.tgt_text">{{ seg.tgt_text || "—" }}</div>
              </td>
              <td class="px-3 py-2">
                <template v-if="voiceProfiles?.length">
                  <div
                    v-if="pendingBinding[seg.speaker_label] !== undefined"
                    class="flex items-center gap-1"
                  >
                    <select
                      :value="pendingBinding[seg.speaker_label]"
                      class="hd-select py-1 text-[10px]"
                      @wheel.prevent
                      @change="
                        pendingBinding[seg.speaker_label] = Number(
                          ($event.target as HTMLSelectElement).value,
                        )
                      "
                    >
                      <option value="">—</option>
                      <option v-for="vp in voiceProfiles" :key="vp.id" :value="vp.id">
                        {{ vp.name }}
                      </option>
                    </select>
                    <button
                      class="hd-icon-btn h-6 w-6 shrink-0"
                      :disabled="bindingSaving[seg.speaker_label]"
                      :title="t('保存绑定')"
                      @click="commitBinding(seg.speaker_label)"
                    >
                      <Check :size="12" />
                    </button>
                    <button
                      class="hd-icon-btn h-6 w-6 shrink-0"
                      :title="t('取消')"
                      @click="delete pendingBinding[seg.speaker_label]"
                    >
                      <X :size="12" />
                    </button>
                  </div>
                  <div v-else class="flex items-center gap-1">
                    <span class="text-[10px] text-white truncate flex-1">{{
                      voiceNameForSpeaker(seg.speaker_label)
                    }}</span>
                    <button
                      class="hd-icon-btn h-6 w-6 shrink-0"
                      :title="t('修改绑定')"
                      @click="startBindingEdit(seg.speaker_label)"
                    >
                      <Pencil :size="11" />
                    </button>
                  </div>
                </template>
                <span v-else class="text-[10px] text-[color:var(--hd-text-dim)]">{{
                  seg.speaker_label || "—"
                }}</span>
              </td>
              <td class="px-3 py-2 text-center">
                <span class="hd-chip justify-center font-mono" :class="driftClass(seg)">
                  {{ driftLabel(seg) }}
                </span>
              </td>
              <td class="px-3 py-2 text-center">
                <span class="hd-chip justify-center font-mono" :class="driftSecClass(seg)">
                  {{ driftSecLabel(seg) }}
                </span>
              </td>
              <!-- OPT-002 judge column: only render when feature is in use. -->
              <td v-if="hasAnyJudgeScore" class="px-3 py-2 text-center">
                <span
                  v-if="seg.judge_score != null"
                  class="hd-chip justify-center font-mono"
                  :class="judgeBadgeClass(seg.judge_score)"
                  :title="judgeTooltip(seg)"
                  >{{ Math.round(seg.judge_score * 100) }}</span
                >
                <span v-else class="text-[10px] text-[color:var(--hd-text-dim)]">—</span>
              </td>
              <td class="px-3 py-2">
                <div class="flex flex-col gap-1">
                  <!-- 原声 -->
                  <div class="flex items-center gap-1">
                    <span class="text-[10px] text-[color:var(--hd-text-dim)] w-6 shrink-0">{{
                      t("原声")
                    }}</span>
                    <audio
                      v-if="originalAudioUrls[seg.id]"
                      :src="originalAudioUrls[seg.id]"
                      controls
                      preload="none"
                      class="h-6 flex-1 min-w-0"
                    ></audio>
                    <button
                      v-else
                      class="flex-1 rounded-[6px] border border-[color:var(--hd-border-soft)] bg-white/[0.04] py-0.5 text-center text-[10px] text-[color:var(--hd-text-muted)] transition-colors hover:text-white disabled:opacity-50"
                      :disabled="loadingOriginalAudio[seg.id]"
                      @click="loadOriginalAudio(seg)"
                    >
                      <LoaderCircle
                        v-if="loadingOriginalAudio[seg.id]"
                        :size="12"
                        class="mx-auto animate-spin"
                      />
                      <Play v-else :size="12" class="mx-auto" />
                    </button>
                  </div>
                  <!-- TTS -->
                  <div class="flex items-center gap-1">
                    <span class="text-[10px] text-[color:var(--hd-text-dim)] w-6 shrink-0"
                      >TTS</span
                    >
                    <template v-if="seg.tts_duration_ms && seg.status === 'synthesized'">
                      <audio
                        v-if="audioUrls[seg.id]"
                        :src="audioUrls[seg.id]"
                        controls
                        preload="none"
                        class="h-6 flex-1 min-w-0"
                      ></audio>
                      <button
                        v-else
                        class="flex-1 rounded-[6px] border border-[color:var(--hd-border-soft)] bg-white/[0.04] py-0.5 text-center text-[10px] text-[color:var(--hd-text-muted)] transition-colors hover:text-white disabled:opacity-50"
                        :disabled="loadingAudio[seg.id]"
                        @click="loadAudio(seg)"
                      >
                        <LoaderCircle
                          v-if="loadingAudio[seg.id]"
                          :size="12"
                          class="mx-auto animate-spin"
                        />
                        <Play v-else :size="12" class="mx-auto" />
                      </button>
                    </template>
                    <span v-else class="text-[color:var(--hd-text-dim)] text-[10px]">—</span>
                  </div>
                </div>
              </td>
              <td class="px-3 py-2 text-center">
                <div class="flex items-center justify-center gap-1">
                  <button class="hd-btn hd-btn-secondary px-2 py-1" @click="toggleEdit(seg)">
                    {{ editingId === seg.id ? t("收起") : t("编辑") }}
                  </button>
                  <button
                    v-if="seg.status === 'synthesized'"
                    class="hd-icon-btn h-7 w-7"
                    :disabled="rerunning[seg.id]"
                    @click="rerunSegment(seg)"
                    :title="t('重新合成')"
                  >
                    <LoaderCircle v-if="rerunning[seg.id]" :size="13" class="animate-spin" />
                    <RotateCcw v-else :size="13" />
                  </button>
                  <button
                    class="hd-btn hd-btn-ghost px-2 py-1"
                    :class="{ 'bg-cyan-300/10 text-cyan-100': waveformSegId === seg.id }"
                    :title="t('波形')"
                    @click="toggleWaveform(seg)"
                  >
                    <Waves :size="13" />
                    {{ t("波形") }}
                  </button>
                </div>
              </td>
            </tr>

            <!-- 波形展开行 -->
            <tr
              v-if="waveformSegId === seg.id"
              class="border-t border-[color:var(--hd-border-soft)] bg-black/20"
            >
              <td :colspan="hasAnyJudgeScore ? 10 : 9" class="px-4 py-3">
                <div class="flex gap-6">
                  <div class="flex-1 min-w-0">
                    <WaveformViewer
                      v-if="originalAudioUrls[seg.id]"
                      :audio-url="originalAudioUrls[seg.id]"
                      :label="t('原声')"
                    />
                    <div v-else class="py-4 text-center text-[color:var(--hd-text-dim)] text-xs">
                      <span v-if="loadingOriginalAudio[seg.id]">{{ t("加载原声中…") }}</span>
                      <span v-else>{{ t("原声未加载") }}</span>
                    </div>
                  </div>
                  <div class="flex-1 min-w-0">
                    <WaveformViewer
                      v-if="seg.status === 'synthesized' && audioUrls[seg.id]"
                      :audio-url="audioUrls[seg.id]"
                      label="TTS"
                    />
                    <div v-else class="py-4 text-center text-[color:var(--hd-text-dim)] text-xs">
                      <span v-if="seg.status !== 'synthesized'">—</span>
                      <span v-else-if="loadingAudio[seg.id]">{{ t("加载 TTS 中…") }}</span>
                      <span v-else>{{ t("TTS 未加载") }}</span>
                    </div>
                  </div>
                </div>
              </td>
            </tr>

            <!-- 内联编辑行 -->
            <tr
              v-if="editingId === seg.id"
              class="border-t border-[color:var(--hd-border-soft)] bg-black/20"
            >
              <td :colspan="hasAnyJudgeScore ? 10 : 9" class="px-4 py-3">
                <div class="space-y-2">
                  <label class="hd-label block">{{ t("译文（可编辑）") }}</label>
                  <textarea v-model="editText" rows="3" class="hd-textarea resize-none"></textarea>
                  <!-- 段落级音色覆盖 -->
                  <div v-if="voiceProfiles?.length" class="flex items-center gap-2">
                    <label class="text-[10px] text-[color:var(--hd-text-muted)] shrink-0">{{
                      t("音色覆盖（此段）")
                    }}</label>
                    <select
                      v-model="editVoiceProfileId"
                      class="hd-select flex-1 py-1 text-[10px]"
                      @wheel.prevent
                    >
                      <option :value="0">{{ t("— 跟随说话人绑定 —") }}</option>
                      <option v-for="vp in voiceProfiles" :key="vp.id" :value="vp.id">
                        {{ vp.name }}
                      </option>
                    </select>
                    <button
                      class="hd-btn hd-btn-secondary shrink-0 py-1 text-[10px]"
                      :disabled="previewing[seg.id]"
                      @click="previewVoice(seg)"
                    >
                      {{ previewing[seg.id] ? t("合成中…") : t("试听") }}
                    </button>
                  </div>
                  <!-- 试听播放区 -->
                  <div v-if="previewUrls[seg.id]" class="flex items-center gap-2">
                    <span class="text-[10px] text-[color:var(--hd-text-muted)] shrink-0">{{
                      t("试听结果")
                    }}</span>
                    <audio
                      :src="previewUrls[seg.id]"
                      controls
                      preload="none"
                      class="h-6 flex-1 min-w-0"
                    />
                    <span class="text-[10px] text-[color:var(--hd-text-dim)]">{{
                      previewDurations[seg.id]
                        ? (previewDurations[seg.id]! / 1000).toFixed(2) + "s"
                        : ""
                    }}</span>
                  </div>
                  <div v-if="previewError[seg.id]" class="text-[10px] text-red-400">
                    {{ t("试听失败:") }} {{ previewError[seg.id] }}
                  </div>
                  <div class="flex gap-2">
                    <button
                      class="hd-btn hd-btn-secondary"
                      :disabled="saving[seg.id]"
                      @click="saveEdit(seg, false)"
                    >
                      {{ saving[seg.id] ? t("保存中...") : t("仅保存") }}
                    </button>
                    <button
                      class="hd-btn hd-btn-primary"
                      :disabled="saving[seg.id]"
                      @click="saveEdit(seg, true)"
                    >
                      {{ saving[seg.id] ? t("处理中...") : t("保存 + 重新合成") }}
                    </button>
                    <button class="hd-btn hd-btn-ghost" @click="cancelEdit">{{ t("取消") }}</button>
                  </div>
                  <div v-if="saveError" class="text-xs text-red-400">{{ saveError }}</div>
                  <div class="text-[10px] text-[color:var(--hd-text-dim)]">
                    {{ t("时间") }}: {{ fmtMs(seg.start_ms) }} → {{ fmtMs(seg.end_ms) }} ·
                    {{ t("原始时长") }}: {{ fmtMs(seg.original_duration_ms) }} · {{ t("TTS时长") }}:
                    {{ seg.tts_duration_ms ? fmtMs(seg.tts_duration_ms) : "—" }}
                  </div>
                </div>
              </td>
            </tr>
          </template>

          <tr v-if="!segments.length">
            <td
              :colspan="hasAnyJudgeScore ? 10 : 9"
              class="px-4 py-8 text-center text-[color:var(--hd-text-dim)]"
            >
              {{ t("暂无段落数据") }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onBeforeUnmount } from "vue";
import { Check, LoaderCircle, Pencil, Play, RotateCcw, Waves, X } from "@lucide/vue";
import { api, type Segment, getApiKey } from "../api";
import { useI18n } from "../i18n";
import WaveformViewer from "./WaveformViewer.vue";

const { t } = useI18n();

const props = defineProps<{
  segments: Segment[];
  jobId: number;
  bindings?: { speaker?: { label: string }; voice_profile_id: number }[];
  voiceProfiles?: { id: number; name: string }[];
}>();

const emit = defineEmits<{
  updated: [];
  "segment-updated": [segment: Segment];
  "binding-updated": [speakerLabel: string, voiceProfileId: number];
}>();

const editingId = ref<number | null>(null);
const waveformSegId = ref<number | null>(null);
const editText = ref("");
const editVoiceProfileId = ref<number>(0);
const saving = ref<Record<number, boolean>>({});
const rerunning = ref<Record<number, boolean>>({});
const batchRerunning = ref(false);
const saveError = ref("");
const audioUrls = ref<Record<number, string>>({});
const loadingAudio = ref<Record<number, boolean>>({});
const originalAudioUrls = ref<Record<number, string>>({});
const loadingOriginalAudio = ref<Record<number, boolean>>({});
const selectedIds = ref<Set<number>>(new Set());
const focusedIndex = ref<number>(0);
const rowRefs = ref<Record<number, HTMLElement | null>>({});
const tableContainerRef = ref<HTMLElement | null>(null);
const previewing = ref<Record<number, boolean>>({});
const previewUrls = ref<Record<number, string>>({});
const previewDurations = ref<Record<number, number | null>>({});
const previewError = ref<Record<number, string>>({});

const synthesizableSegments = computed(() =>
  props.segments.filter((s) => s.status === "synthesized"),
);
const allSelected = computed(() => {
  const syn = synthesizableSegments.value;
  if (!syn.length) return false;
  return syn.every((s) => selectedIds.value.has(s.id));
});
const someSelected = computed(() => selectedIds.value.size > 0 && !allSelected.value);

function bindingProfileId(speakerLabel: string): number | string {
  if (!props.bindings?.length || !speakerLabel) return "";
  const b = props.bindings.find((x) => x.speaker?.label === speakerLabel);
  return b?.voice_profile_id ?? "";
}

const bindingSaving = ref<Record<string, boolean>>({});
const pendingBinding = ref<Record<string, number | string>>({});

function voiceNameForSpeaker(speakerLabel: string): string {
  const id = bindingProfileId(speakerLabel);
  if (!id) return speakerLabel || "—";
  const vp = props.voiceProfiles?.find((v) => v.id === Number(id));
  return vp ? vp.name : speakerLabel || "—";
}

function startBindingEdit(speakerLabel: string) {
  pendingBinding.value[speakerLabel] = bindingProfileId(speakerLabel);
}

async function commitBinding(speakerLabel: string) {
  const vpId = Number(pendingBinding.value[speakerLabel]);
  delete pendingBinding.value[speakerLabel];
  await onVoiceChange(speakerLabel, vpId);
}

async function onVoiceChange(speakerLabel: string, voiceProfileId: number) {
  if (!voiceProfileId || !speakerLabel) return;
  bindingSaving.value[speakerLabel] = true;
  try {
    await api.upsertBindings(
      props.jobId,
      [{ speaker_label: speakerLabel, voice_profile_id: voiceProfileId }],
      false,
    );
    emit("binding-updated", speakerLabel, voiceProfileId);
  } catch (e: unknown) {
    alert(e instanceof Error ? e.message : String(e));
  } finally {
    bindingSaving.value[speakerLabel] = false;
  }
}

function setRowRef(ordinal: number, el: unknown) {
  rowRefs.value[ordinal] = el as HTMLElement | null;
}

function toggleSelect(seg: Segment) {
  const next = new Set(selectedIds.value);
  if (next.has(seg.id)) next.delete(seg.id);
  else next.add(seg.id);
  selectedIds.value = next;
}

function toggleSelectAll() {
  const syn = synthesizableSegments.value;
  if (allSelected.value) selectedIds.value = new Set();
  else selectedIds.value = new Set(syn.map((s) => s.id));
}

function clearSelection() {
  selectedIds.value = new Set();
}

async function batchRerun() {
  const ids = Array.from(selectedIds.value);
  if (!ids.length) return;
  batchRerunning.value = true;
  try {
    await api.retryJob(props.jobId, "tts_duration", ids);
    ids.forEach((id) => {
      if (audioUrls.value[id]) {
        URL.revokeObjectURL(audioUrls.value[id]);
        delete audioUrls.value[id];
      }
    });
    selectedIds.value = new Set();
    emit("updated");
  } catch (e: unknown) {
    alert(e instanceof Error ? e.message : String(e));
  } finally {
    batchRerunning.value = false;
  }
}

function onKeydown(e: KeyboardEvent) {
  if (props.segments.length === 0) return;
  const target = e.target as HTMLElement;
  if (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable)
    return;
  const len = props.segments.length;
  const idx = Math.max(0, Math.min(focusedIndex.value, len - 1));
  const seg = props.segments[idx];
  if (e.key === "j" || e.key === "J") {
    e.preventDefault();
    focusedIndex.value = Math.min(idx + 1, len - 1);
    scrollToFocused();
  } else if (e.key === "k" || e.key === "K") {
    e.preventDefault();
    focusedIndex.value = Math.max(0, idx - 1);
    scrollToFocused();
  } else if (e.key === " ") {
    e.preventDefault();
    if (seg?.status === "synthesized") {
      if (audioUrls.value[seg.id]) {
        const el = document.querySelector(
          `audio[src="${audioUrls.value[seg.id]}"]`,
        ) as HTMLAudioElement;
        if (el) {
          if (el.paused) el.play();
          else el.pause();
        }
      } else loadAudio(seg);
    }
  } else if (e.key === "e" || e.key === "E") {
    e.preventDefault();
    if (seg) toggleEdit(seg);
  }
}

function scrollToFocused() {
  const seg = props.segments[focusedIndex.value];
  if (!seg) return;
  const el = rowRefs.value[seg.ordinal];
  el?.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

function focusRow(seg: Segment, e?: MouseEvent) {
  const target = e?.target as HTMLElement | null;
  const INTERACTIVE = ["SELECT", "INPUT", "TEXTAREA", "BUTTON", "AUDIO"];
  if (
    target &&
    (INTERACTIVE.includes(target.tagName) || target.closest("select,input,textarea,button,audio"))
  ) {
    return;
  }
  const i = props.segments.findIndex((s) => s.id === seg.id);
  if (i >= 0) focusedIndex.value = i;
  tableContainerRef.value?.focus();
}

watch(
  () => props.segments.length,
  () => {
    focusedIndex.value = Math.min(focusedIndex.value, Math.max(0, props.segments.length - 1));
  },
);

function driftPct(seg: Segment): number {
  if (!seg.tts_duration_ms || !seg.original_duration_ms) return -1;
  return (
    (Math.abs(seg.tts_duration_ms - seg.original_duration_ms) / seg.original_duration_ms) * 100
  );
}

function driftClass(seg: Segment): string {
  if (seg.status !== "synthesized") return "hd-chip-muted";
  const pct = driftPct(seg);
  if (pct < 0) return "hd-chip-muted";
  if (pct < 5) return "hd-chip-success";
  if (pct < 15) return "hd-chip-warning";
  return "hd-chip-danger";
}

function driftLabel(seg: Segment): string {
  if (seg.status !== "synthesized") return "—";
  const pct = driftPct(seg);
  if (pct < 0) return "—";
  return `${pct.toFixed(1)}%`;
}

// Signed drift in seconds: positive = TTS too long, negative = TTS too short.
function driftSec(seg: Segment): number | null {
  if (seg.status !== "synthesized" || !seg.tts_duration_ms || !seg.original_duration_ms)
    return null;
  return (seg.tts_duration_ms - seg.original_duration_ms) / 1000;
}

function driftSecLabel(seg: Segment): string {
  const d = driftSec(seg);
  if (d === null) return "—";
  const sign = d >= 0 ? "+" : "";
  return `${sign}${d.toFixed(2)}s`;
}

function driftSecClass(seg: Segment): string {
  if (seg.status !== "synthesized") return "hd-chip-muted";
  const d = driftSec(seg);
  if (d === null) return "hd-chip-muted";
  const abs = Math.abs(d);
  if (abs < 0.5) return "hd-chip-success";
  if (abs < 1.5) return "hd-chip-warning";
  return "hd-chip-danger";
}

// OPT-002: LLM-as-Judge column. Only show the column when at least one
// segment has been judged (so jobs predating the feature look unchanged).
const hasAnyJudgeScore = computed(() => props.segments.some((s) => s.judge_score != null));

function judgeBadgeClass(score: number): string {
  if (score >= 0.8) return "hd-chip-success";
  if (score >= 0.6) return "hd-chip-warning";
  return "hd-chip-danger";
}

function judgeTooltip(seg: Segment): string {
  const m = seg.judge_meta;
  if (!m) return `score=${seg.judge_score}`;
  const parts: string[] = [];
  if (m.fidelity != null) parts.push(`fid=${m.fidelity.toFixed(2)}`);
  if (m.fluency != null) parts.push(`flu=${m.fluency.toFixed(2)}`);
  if (m.coherence != null) parts.push(`coh=${m.coherence.toFixed(2)}`);
  if (m.verdict) parts.push(`verdict=${m.verdict}`);
  if (m.issues && m.issues.length > 0) parts.push(`issues: ${m.issues.join("; ")}`);
  return parts.join(" | ");
}

function fmtMs(ms: number): string {
  const s = ms / 1000;
  const m = Math.floor(s / 60);
  const rem = (s % 60).toFixed(1);
  return m > 0 ? `${m}:${rem.padStart(4, "0")}` : `${rem}s`;
}

function toggleWaveform(seg: Segment) {
  if (waveformSegId.value === seg.id) {
    waveformSegId.value = null;
    return;
  }
  editingId.value = null;
  waveformSegId.value = seg.id;
  if (!originalAudioUrls.value[seg.id] && !loadingOriginalAudio.value[seg.id]) {
    loadOriginalAudio(seg);
  }
  if (seg.status === "synthesized" && !audioUrls.value[seg.id] && !loadingAudio.value[seg.id]) {
    loadAudio(seg);
  }
}

function toggleEdit(seg: Segment) {
  if (editingId.value === seg.id) {
    cancelEdit();
    return;
  }
  waveformSegId.value = null;
  editingId.value = seg.id;
  editText.value = seg.tgt_text || "";
  editVoiceProfileId.value = seg.voice_profile_id ?? 0;
  saveError.value = "";
}

function cancelEdit() {
  editingId.value = null;
  editText.value = "";
  editVoiceProfileId.value = 0;
  saveError.value = "";
}

async function saveEdit(seg: Segment, rerun: boolean) {
  saving.value[seg.id] = true;
  saveError.value = "";
  try {
    // Pass voice override if it changed
    const voiceChanged = editVoiceProfileId.value !== (seg.voice_profile_id ?? 0);
    const voiceArg = voiceChanged ? editVoiceProfileId.value : undefined;
    await api.patchSegment(props.jobId, seg.id, editText.value, rerun, voiceArg);
    cancelEdit();
    // Invalidate cached audio if rerunning
    if (rerun && audioUrls.value[seg.id]) {
      URL.revokeObjectURL(audioUrls.value[seg.id]);
      delete audioUrls.value[seg.id];
    }
    emit("updated");
  } catch (e: unknown) {
    saveError.value = e instanceof Error ? e.message : String(e);
  } finally {
    saving.value[seg.id] = false;
  }
}

async function previewVoice(seg: Segment) {
  const vpId = editVoiceProfileId.value || (seg.voice_profile_id ?? 0);
  if (!vpId) {
    previewError.value[seg.id] = t("请先选择一个音色");
    return;
  }
  previewing.value[seg.id] = true;
  previewError.value[seg.id] = "";
  // Revoke old preview URL
  if (previewUrls.value[seg.id]) {
    URL.revokeObjectURL(previewUrls.value[seg.id]);
    delete previewUrls.value[seg.id];
  }
  try {
    await api.previewVoice(props.jobId, seg.id, vpId);
    // Fetch the audio via the serve endpoint
    const headers: Record<string, string> = {};
    const key = getApiKey();
    if (key) headers["X-API-Key"] = key;
    const resp = await fetch(`/jobs/${props.jobId}/preview-voice/${seg.id}?vp=${vpId}`, {
      headers,
    });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const blob = await resp.blob();
    previewUrls.value[seg.id] = URL.createObjectURL(blob);
    // Estimate duration from blob size (rough; will be updated)
    previewDurations.value[seg.id] = null;
  } catch (e: unknown) {
    previewError.value[seg.id] = e instanceof Error ? e.message : String(e);
  } finally {
    previewing.value[seg.id] = false;
  }
}

async function rerunSegment(seg: Segment) {
  rerunning.value[seg.id] = true;
  try {
    await api.rerunSegment(props.jobId, seg.id);
    if (audioUrls.value[seg.id]) {
      URL.revokeObjectURL(audioUrls.value[seg.id]);
      delete audioUrls.value[seg.id];
    }
    emit("updated");
  } catch (e: unknown) {
    alert(e instanceof Error ? e.message : String(e));
  } finally {
    rerunning.value[seg.id] = false;
  }
}

async function loadAudio(seg: Segment) {
  loadingAudio.value[seg.id] = true;
  try {
    const headers: Record<string, string> = {};
    const key = getApiKey();
    if (key) headers["X-API-Key"] = key;

    const resp = await fetch(`/jobs/${props.jobId}/tts/${seg.ordinal}`, { headers });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const blob = await resp.blob();
    audioUrls.value[seg.id] = URL.createObjectURL(blob);
  } catch (e: unknown) {
    alert(t("TTS 加载失败: {message}", { message: e instanceof Error ? e.message : String(e) }));
  } finally {
    loadingAudio.value[seg.id] = false;
  }
}

async function loadOriginalAudio(seg: Segment) {
  loadingOriginalAudio.value[seg.id] = true;
  try {
    const headers: Record<string, string> = {};
    const key = getApiKey();
    if (key) headers["X-API-Key"] = key;

    const resp = await fetch(`/jobs/${props.jobId}/audio/${seg.ordinal}`, { headers });
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const blob = await resp.blob();
    originalAudioUrls.value[seg.id] = URL.createObjectURL(blob);
  } catch (e: unknown) {
    alert(t("原声加载失败: {message}", { message: e instanceof Error ? e.message : String(e) }));
  } finally {
    loadingOriginalAudio.value[seg.id] = false;
  }
}

onBeforeUnmount(() => {
  for (const url of Object.values(audioUrls.value)) {
    if (url) URL.revokeObjectURL(url);
  }
  for (const url of Object.values(originalAudioUrls.value)) {
    if (url) URL.revokeObjectURL(url);
  }
  for (const url of Object.values(previewUrls.value)) {
    if (url) URL.revokeObjectURL(url);
  }
});
</script>
