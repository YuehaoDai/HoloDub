<template>
  <div class="overflow-x-auto rounded-lg border border-[#1e2535]">
    <table class="w-full text-xs border-collapse">
      <thead>
        <tr class="bg-[#171b26] text-[#9db0c9]">
          <th class="px-3 py-2 text-left font-medium w-10">#</th>
          <th class="px-3 py-2 text-left font-medium">原文</th>
          <th class="px-3 py-2 text-left font-medium">译文</th>
          <th class="px-3 py-2 text-center font-medium w-20">漂移率</th>
          <th class="px-3 py-2 text-center font-medium w-28">试听</th>
          <th class="px-3 py-2 text-center font-medium w-16">操作</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="seg in segments" :key="seg.id">
          <tr
            class="border-t border-[#1e2535] hover:bg-[#1a2030] transition-colors"
            :class="{ 'bg-[#1e2535]/30': editingId === seg.id }"
          >
            <td class="px-3 py-2 text-[#9db0c9] font-mono">{{ seg.ordinal }}</td>
            <td class="px-3 py-2 text-[#9db0c9] max-w-xs">
              <div class="truncate" :title="seg.src_text">{{ seg.src_text }}</div>
            </td>
            <td class="px-3 py-2 text-[#f2f5f7] max-w-xs">
              <div class="truncate" :title="seg.tgt_text">{{ seg.tgt_text || '—' }}</div>
            </td>
            <td class="px-3 py-2 text-center">
              <span class="inline-block px-1.5 py-0.5 rounded-full text-[10px] font-medium" :class="driftClass(seg)">
                {{ driftLabel(seg) }}
              </span>
            </td>
            <td class="px-3 py-2">
              <div v-if="seg.tts_duration_ms && seg.status === 'synthesized'">
                <audio
                  v-if="audioUrls[seg.id]"
                  :src="audioUrls[seg.id]"
                  controls
                  preload="none"
                  class="h-7 w-full"
                ></audio>
                <button
                  v-else
                  class="w-full text-center py-1 rounded bg-[#1e2535] hover:bg-[#273246] text-[#9db0c9] hover:text-white transition-colors"
                  :disabled="loadingAudio[seg.id]"
                  @click="loadAudio(seg)"
                >
                  {{ loadingAudio[seg.id] ? '…' : '▶ 加载' }}
                </button>
              </div>
              <span v-else class="text-[#37465f]">—</span>
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
              </div>
            </td>
          </tr>

          <!-- 内联编辑行 -->
          <tr v-if="editingId === seg.id" class="border-t border-[#273246] bg-[#111722]">
            <td colspan="6" class="px-4 py-3">
              <div class="space-y-2">
                <label class="block text-[10px] text-[#9db0c9]">译文（可编辑）</label>
                <textarea
                  v-model="editText"
                  rows="3"
                  class="w-full text-xs px-2 py-1.5 rounded bg-[#1e2535] border border-[#273246] text-[#f2f5f7] placeholder-[#37465f] focus:outline-none focus:border-[#4a6080] resize-none"
                ></textarea>
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
          <td colspan="6" class="px-4 py-8 text-center text-[#37465f]">暂无段落数据</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue";
import { api, type Segment, getApiKey } from "../api";

const props = defineProps<{
  segments: Segment[];
  jobId: number;
}>();

const emit = defineEmits<{ updated: [] }>();

const editingId = ref<number | null>(null);
const editText = ref("");
const saving = ref<Record<number, boolean>>({});
const rerunning = ref<Record<number, boolean>>({});
const saveError = ref("");
const audioUrls = ref<Record<number, string>>({});
const loadingAudio = ref<Record<number, boolean>>({});

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

function fmtMs(ms: number): string {
  const s = ms / 1000;
  const m = Math.floor(s / 60);
  const rem = (s % 60).toFixed(1);
  return m > 0 ? `${m}:${rem.padStart(4, "0")}` : `${rem}s`;
}

function toggleEdit(seg: Segment) {
  if (editingId.value === seg.id) {
    cancelEdit();
    return;
  }
  editingId.value = seg.id;
  editText.value = seg.tgt_text || "";
  saveError.value = "";
}

function cancelEdit() {
  editingId.value = null;
  editText.value = "";
  saveError.value = "";
}

async function saveEdit(seg: Segment, rerun: boolean) {
  saving.value[seg.id] = true;
  saveError.value = "";
  try {
    await api.patchSegment(props.jobId, seg.id, editText.value, rerun);
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
    alert(`音频加载失败: ${e instanceof Error ? e.message : String(e)}`);
  } finally {
    loadingAudio.value[seg.id] = false;
  }
}
</script>
