<template>
  <div
    ref="tableContainerRef"
    tabindex="0"
    class="outline-none"
    @keydown="onKeydown"
  >
    <!-- 批量操作栏 -->
    <div
      v-if="selectedIds.size > 0"
      class="flex items-center gap-3 mb-3 px-3 py-2 rounded-lg bg-[#1e2535] border border-[#273246] text-xs"
    >
      <span class="text-[#9db0c9]">已选 {{ selectedIds.size }} 段</span>
      <button
        class="px-3 py-1.5 rounded bg-blue-600 hover:bg-blue-500 text-white font-medium disabled:opacity-50"
        :disabled="batchRerunning"
        @click="batchRerun"
      >
        {{ batchRerunning ? "提交中…" : "批量重合成" }}
      </button>
      <button
        class="px-2 py-1 rounded text-[#9db0c9] hover:bg-[#273246] hover:text-white"
        @click="clearSelection"
      >
        取消选择
      </button>
    </div>
    <div class="overflow-x-auto rounded-lg border border-[#1e2535]">
      <table class="w-full text-xs border-collapse">
        <thead>
          <tr class="bg-[#171b26] text-[#9db0c9]">
            <th class="px-2 py-2 text-center w-8">
              <input
                type="checkbox"
                :checked="allSelected"
                :indeterminate="someSelected"
                class="rounded border-[#37465f] bg-[#1e2535]"
                @change="toggleSelectAll"
              />
            </th>
            <th class="px-3 py-2 text-left font-medium w-10">#</th>
          <th class="px-3 py-2 text-left font-medium">原文</th>
          <th class="px-3 py-2 text-left font-medium">译文</th>
          <th class="px-3 py-2 text-center font-medium w-24">声音</th>
          <th class="px-3 py-2 text-center font-medium w-20">漂移率</th>
          <th class="px-3 py-2 text-center font-medium w-20">偏差时长</th>
          <th class="px-3 py-2 text-center font-medium w-40">原声 / TTS</th>
          <th class="px-3 py-2 text-center font-medium w-16">操作</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="(seg, index) in segments" :key="seg.id">
          <tr
            class="border-t border-[#1e2535] hover:bg-[#1a2030] transition-colors"
            :class="{
              'bg-[#1e2535]/30': editingId === seg.id,
              'bg-[#1e2535]/20': selectedIds.has(seg.id),
              'ring-1 ring-blue-500/50': focusedIndex === index
            }"
            :ref="(el) => setRowRef(seg.ordinal, el)"
            @click="focusRow(seg, $event)"
          >
            <td class="px-2 py-2 text-center">
              <input
                v-if="seg.status === 'synthesized'"
                type="checkbox"
                :checked="selectedIds.has(seg.id)"
                class="rounded border-[#37465f] bg-[#1e2535]"
                @change="toggleSelect(seg)"
              />
              <span v-else class="text-[#37465f]">—</span>
            </td>
            <td class="px-3 py-2 text-[#9db0c9] font-mono">{{ seg.ordinal }}</td>
            <td class="px-3 py-2 text-[#9db0c9] max-w-xs">
              <div class="truncate" :title="seg.src_text">{{ seg.src_text }}</div>
            </td>
            <td class="px-3 py-2 text-[#f2f5f7] max-w-xs">
              <div class="truncate" :title="seg.tgt_text">{{ seg.tgt_text || '—' }}</div>
            </td>
            <td class="px-3 py-2">
              <template v-if="voiceProfiles?.length">
                <div v-if="pendingBinding[seg.speaker_label] !== undefined" class="flex items-center gap-1">
                  <select
                    :value="pendingBinding[seg.speaker_label]"
                    class="w-full text-[10px] px-1 py-0.5 rounded bg-[#1e2535] border border-amber-500 text-[#f2f5f7]"
                    @wheel.prevent
                    @change="pendingBinding[seg.speaker_label] = Number(($event.target as HTMLSelectElement).value)"
                  >
                    <option value="">—</option>
                    <option v-for="vp in voiceProfiles" :key="vp.id" :value="vp.id">{{ vp.name }}</option>
                  </select>
                  <button
                    class="text-[10px] px-1.5 py-0.5 rounded bg-blue-600 hover:bg-blue-500 text-white shrink-0 disabled:opacity-50"
                    :disabled="bindingSaving[seg.speaker_label]"
                    title="保存绑定"
                    @click="commitBinding(seg.speaker_label)"
                  >✓</button>
                  <button
                    class="text-[10px] px-1.5 py-0.5 rounded bg-[#273246] hover:bg-[#37465f] text-[#9db0c9] shrink-0"
                    title="取消"
                    @click="delete pendingBinding[seg.speaker_label]"
                  >✕</button>
                </div>
                <div v-else class="flex items-center gap-1">
                  <span class="text-[10px] text-[#f2f5f7] truncate flex-1">{{ voiceNameForSpeaker(seg.speaker_label) }}</span>
                  <button
                    class="text-[10px] px-1 py-0.5 rounded bg-[#273246] hover:bg-[#37465f] text-[#9db0c9] shrink-0"
                    title="修改绑定"
                    @click="startBindingEdit(seg.speaker_label)"
                  >✎</button>
                </div>
              </template>
              <span v-else class="text-[10px] text-[#37465f]">{{ seg.speaker_label || '—' }}</span>
            </td>
            <td class="px-3 py-2 text-center">
              <span class="inline-block px-1.5 py-0.5 rounded-full text-[10px] font-medium" :class="driftClass(seg)">
                {{ driftLabel(seg) }}
              </span>
            </td>
            <td class="px-3 py-2 text-center">
              <span class="inline-block px-1.5 py-0.5 rounded-full text-[10px] font-medium font-mono" :class="driftSecClass(seg)">
                {{ driftSecLabel(seg) }}
              </span>
            </td>
            <td class="px-3 py-2">
              <div class="flex flex-col gap-1">
                <!-- 原声 -->
                <div class="flex items-center gap-1">
                  <span class="text-[10px] text-[#37465f] w-6 shrink-0">原声</span>
                  <audio
                    v-if="originalAudioUrls[seg.id]"
                    :src="originalAudioUrls[seg.id]"
                    controls
                    preload="none"
                    class="h-6 flex-1 min-w-0"
                  ></audio>
                  <button
                    v-else
                    class="flex-1 text-center py-0.5 rounded bg-[#1e2535] hover:bg-[#273246] text-[10px] text-[#9db0c9] hover:text-white transition-colors"
                    :disabled="loadingOriginalAudio[seg.id]"
                    @click="loadOriginalAudio(seg)"
                  >
                    {{ loadingOriginalAudio[seg.id] ? '…' : '▶' }}
                  </button>
                </div>
                <!-- TTS -->
                <div class="flex items-center gap-1">
                  <span class="text-[10px] text-[#37465f] w-6 shrink-0">TTS</span>
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
                      class="flex-1 text-center py-0.5 rounded bg-[#1e2535] hover:bg-[#273246] text-[10px] text-[#9db0c9] hover:text-white transition-colors"
                      :disabled="loadingAudio[seg.id]"
                      @click="loadAudio(seg)"
                    >
                      {{ loadingAudio[seg.id] ? '…' : '▶' }}
                    </button>
                  </template>
                  <span v-else class="text-[#37465f] text-[10px]">—</span>
                </div>
              </div>
            </td>
            <td class="px-3 py-2 text-center">
              <div class="flex items-center justify-center gap-1">
                <button
                  class="px-2 py-1 rounded bg-[#273246] hover:bg-[#37465f] text-[#9db0c9] hover:text-white transition-colors"
                  @click="toggleEdit(seg)"
                >
                  {{ editingId === seg.id ? '收起' : '编辑' }}
                </button>
                <button
                  v-if="seg.status === 'synthesized'"
                  class="px-2 py-1 rounded bg-[#1e2535] hover:bg-[#273246] text-[#9db0c9] hover:text-white transition-colors"
                  :disabled="rerunning[seg.id]"
                  @click="rerunSegment(seg)"
                  title="重新合成"
                >
                  {{ rerunning[seg.id] ? '…' : '↻' }}
                </button>
                <button
                  class="px-2 py-1 rounded bg-[#1e2535] hover:bg-[#273246] text-[#9db0c9] hover:text-white transition-colors"
                  :class="{ 'bg-[#273246]': waveformSegId === seg.id }"
                  title="波形"
                  @click="toggleWaveform(seg)"
                >
                  波形
                </button>
              </div>
            </td>
          </tr>

          <!-- 波形展开行 -->
          <tr v-if="waveformSegId === seg.id" class="border-t border-[#273246] bg-[#111722]">
            <td colspan="9" class="px-4 py-3">
              <div class="flex gap-6">
                <div class="flex-1 min-w-0">
                  <WaveformViewer
                    v-if="originalAudioUrls[seg.id]"
                    :audio-url="originalAudioUrls[seg.id]"
                    label="原声"
                  />
                  <div v-else class="py-4 text-center text-[#37465f] text-xs">
                    <span v-if="loadingOriginalAudio[seg.id]">加载原声中…</span>
                    <span v-else>原声未加载</span>
                  </div>
                </div>
                <div class="flex-1 min-w-0">
                  <WaveformViewer
                    v-if="seg.status === 'synthesized' && audioUrls[seg.id]"
                    :audio-url="audioUrls[seg.id]"
                    label="TTS"
                  />
                  <div v-else class="py-4 text-center text-[#37465f] text-xs">
                    <span v-if="seg.status !== 'synthesized'">—</span>
                    <span v-else-if="loadingAudio[seg.id]">加载 TTS 中…</span>
                    <span v-else>TTS 未加载</span>
                  </div>
                </div>
              </div>
            </td>
          </tr>

          <!-- 内联编辑行 -->
          <tr v-if="editingId === seg.id" class="border-t border-[#273246] bg-[#111722]">
            <td colspan="9" class="px-4 py-3">
              <div class="space-y-2">
                <label class="block text-[10px] text-[#9db0c9]">译文（可编辑）</label>
                <textarea
                  v-model="editText"
                  rows="3"
                  class="w-full text-xs px-2 py-1.5 rounded bg-[#1e2535] border border-[#273246] text-[#f2f5f7] placeholder-[#37465f] focus:outline-none focus:border-[#4a6080] resize-none"
                ></textarea>
                <!-- 段落级音色覆盖 -->
                <div v-if="voiceProfiles?.length" class="flex items-center gap-2">
                  <label class="text-[10px] text-[#9db0c9] shrink-0">音色覆盖（此段）</label>
                  <select
                    v-model="editVoiceProfileId"
                    class="text-[10px] px-1 py-0.5 rounded bg-[#1e2535] border border-[#273246] text-[#f2f5f7] flex-1"
                    @wheel.prevent
                  >
                    <option :value="0">— 跟随说话人绑定 —</option>
                    <option v-for="vp in voiceProfiles" :key="vp.id" :value="vp.id">{{ vp.name }}</option>
                  </select>
                  <button
                    class="text-[10px] px-2 py-1 rounded bg-[#273246] hover:bg-[#37465f] text-[#9db0c9] hover:text-white transition-colors disabled:opacity-50 shrink-0"
                    :disabled="previewing[seg.id]"
                    @click="previewVoice(seg)"
                  >
                    {{ previewing[seg.id] ? '合成中…' : '试听' }}
                  </button>
                </div>
                <!-- 试听播放区 -->
                <div v-if="previewUrls[seg.id]" class="flex items-center gap-2">
                  <span class="text-[10px] text-[#9db0c9] shrink-0">试听结果</span>
                  <audio :src="previewUrls[seg.id]" controls preload="none" class="h-6 flex-1 min-w-0" />
                  <span class="text-[10px] text-[#37465f]">{{ previewDurations[seg.id] ? (previewDurations[seg.id]! / 1000).toFixed(2) + 's' : '' }}</span>
                </div>
                <div v-if="previewError[seg.id]" class="text-[10px] text-red-400">试听失败: {{ previewError[seg.id] }}</div>
                <div class="flex gap-2">
                  <button
                    class="text-xs px-3 py-1.5 rounded bg-[#273246] hover:bg-[#37465f] text-[#f2f5f7] transition-colors disabled:opacity-50"
                    :disabled="saving[seg.id]"
                    @click="saveEdit(seg, false)"
                  >
                    {{ saving[seg.id] ? '保存中...' : '仅保存' }}
                  </button>
                  <button
                    class="text-xs px-3 py-1.5 rounded bg-blue-600 hover:bg-blue-500 text-white transition-colors disabled:opacity-50"
                    :disabled="saving[seg.id]"
                    @click="saveEdit(seg, true)"
                  >
                    {{ saving[seg.id] ? '处理中...' : '保存 + 重新合成' }}
                  </button>
                  <button
                    class="text-xs px-3 py-1.5 rounded bg-transparent text-[#9db0c9] hover:text-white transition-colors"
                    @click="cancelEdit"
                  >取消</button>
                </div>
                <div v-if="saveError" class="text-xs text-red-400">{{ saveError }}</div>
                <div class="text-[10px] text-[#37465f]">
                  时间: {{ fmtMs(seg.start_ms) }} → {{ fmtMs(seg.end_ms) }} ·
                  原始时长: {{ fmtMs(seg.original_duration_ms) }} ·
                  TTS时长: {{ seg.tts_duration_ms ? fmtMs(seg.tts_duration_ms) : '—' }}
                </div>
              </div>
            </td>
          </tr>
        </template>

        <tr v-if="!segments.length">
          <td colspan="9" class="px-4 py-8 text-center text-[#37465f]">暂无段落数据</td>
        </tr>
      </tbody>
    </table>
  </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onBeforeUnmount } from "vue";
import { api, type Segment, getApiKey } from "../api";
import WaveformViewer from "./WaveformViewer.vue";

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
  props.segments.filter((s) => s.status === "synthesized")
);
const allSelected = computed(() => {
  const syn = synthesizableSegments.value;
  if (!syn.length) return false;
  return syn.every((s) => selectedIds.value.has(s.id));
});
const someSelected = computed(() => selectedIds.value.size > 0 && !allSelected.value);

const qualitySaving = ref<Record<number, boolean>>({});

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
    await api.upsertBindings(props.jobId, [{ speaker_label: speakerLabel, voice_profile_id: voiceProfileId }], false);
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
  if (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable) return;
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
        const el = document.querySelector(`audio[src="${audioUrls.value[seg.id]}"]`) as HTMLAudioElement;
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
  if (target && (INTERACTIVE.includes(target.tagName) || target.closest("select,input,textarea,button,audio"))) {
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
  }
);

function driftPct(seg: Segment): number {
  if (!seg.tts_duration_ms || !seg.original_duration_ms) return -1;
  return (Math.abs(seg.tts_duration_ms - seg.original_duration_ms) / seg.original_duration_ms) * 100;
}

function driftClass(seg: Segment): string {
  if (seg.status !== "synthesized") return "bg-slate-800 text-slate-400";
  const pct = driftPct(seg);
  if (pct < 0) return "bg-slate-800 text-slate-400";
  if (pct < 5) return "bg-green-900 text-green-300";
  if (pct < 15) return "bg-yellow-900 text-yellow-300";
  return "bg-red-900 text-red-300";
}

function driftLabel(seg: Segment): string {
  if (seg.status !== "synthesized") return "—";
  const pct = driftPct(seg);
  if (pct < 0) return "—";
  return `${pct.toFixed(1)}%`;
}

// Signed drift in seconds: positive = TTS too long, negative = TTS too short.
function driftSec(seg: Segment): number | null {
  if (seg.status !== "synthesized" || !seg.tts_duration_ms || !seg.original_duration_ms) return null;
  return (seg.tts_duration_ms - seg.original_duration_ms) / 1000;
}

function driftSecLabel(seg: Segment): string {
  const d = driftSec(seg);
  if (d === null) return "—";
  const sign = d >= 0 ? "+" : "";
  return `${sign}${d.toFixed(2)}s`;
}

function driftSecClass(seg: Segment): string {
  if (seg.status !== "synthesized") return "bg-slate-800 text-slate-400";
  const d = driftSec(seg);
  if (d === null) return "bg-slate-800 text-slate-400";
  const abs = Math.abs(d);
  if (abs < 0.5) return "bg-green-900 text-green-300";
  if (abs < 1.5) return "bg-yellow-900 text-yellow-300";
  return "bg-red-900 text-red-300";
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
    previewError.value[seg.id] = "请先选择一个音色";
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
    const resp = await fetch(`/jobs/${props.jobId}/preview-voice/${seg.id}?vp=${vpId}`, { headers });
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
    alert(`TTS 加载失败: ${e instanceof Error ? e.message : String(e)}`);
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
    alert(`原声加载失败: ${e instanceof Error ? e.message : String(e)}`);
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
