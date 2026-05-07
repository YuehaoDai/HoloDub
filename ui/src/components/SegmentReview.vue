<template>
  <div class="flex flex-col gap-4">
    <!-- LLM Suggestions Panel -->
    <div v-if="suggestions.length > 0" class="rounded-lg border border-yellow-600/40 bg-yellow-900/10 p-4">
      <div class="flex items-center gap-2 mb-3">
        <span class="text-yellow-400 font-semibold text-sm">🤖 AI 分段建议</span>
        <span class="text-xs text-[#9db0c9] ml-auto">{{ pendingSuggestions.length }} 条待处理</span>
      </div>
      <div class="flex flex-col gap-2">
        <div
          v-for="sug in suggestions"
          :key="sug.id"
          class="flex items-start gap-3 px-3 py-2.5 rounded-lg text-xs border"
          :class="{
            'border-[#273246] bg-[#1a2133]': sug.status === 'pending',
            'border-green-800/40 bg-green-900/10 opacity-60': sug.status === 'accepted',
            'border-[#273246] bg-[#12161e] opacity-40': sug.status === 'rejected',
          }"
        >
          <div class="flex-1 min-w-0">
            <div class="flex items-center gap-2 mb-1">
              <span class="px-1.5 py-0.5 rounded text-[10px] font-bold uppercase tracking-wide bg-blue-700/40 text-blue-300">
                合并
              </span>
              <span class="text-[#9db0c9]">
                段 {{ getSegmentOrdinals(sug.segment_ids).join(' + ') }}
              </span>
              <span
                class="ml-auto text-[10px] px-1.5 py-0.5 rounded"
                :class="confidenceClass(sug.confidence)"
              >
                {{ Math.round(sug.confidence * 100) }}% 置信
              </span>
            </div>
            <p class="text-[#c5d4e8] leading-relaxed">{{ sug.reason }}</p>
            <div class="mt-1.5 text-[10px] text-[#6b7f99] flex gap-3">
              <span v-for="sid in sug.segment_ids" :key="sid">
                <template v-if="segmentMap[sid]">
                  [{{ sid }}] {{ truncate(segmentMap[sid].src_text, 30) }}
                </template>
              </span>
            </div>
          </div>
          <div v-if="sug.status === 'pending'" class="flex gap-1.5 shrink-0 mt-0.5">
            <button
              class="px-2.5 py-1 rounded bg-green-700 hover:bg-green-600 text-white font-medium"
              :disabled="actionLoading[sug.id]"
              @click="acceptSuggestion(sug)"
            >
              采纳
            </button>
            <button
              class="px-2.5 py-1 rounded bg-[#273246] hover:bg-[#37465f] text-[#9db0c9]"
              :disabled="actionLoading[sug.id]"
              @click="rejectSuggestion(sug)"
            >
              否决
            </button>
          </div>
          <div v-else class="text-[10px] shrink-0 mt-1 font-semibold"
            :class="sug.status === 'accepted' ? 'text-green-400' : 'text-[#6b7f99]'"
          >
            {{ sug.status === 'accepted' ? '✓ 已采纳' : '✗ 已否决' }}
          </div>
        </div>
      </div>
    </div>
    <div v-else-if="!suggestionsLoading" class="rounded-lg border border-[#1e2535] bg-[#131720] px-4 py-3 text-xs text-[#6b7f99]">
      AI 未发现明显分段问题，请自行检查下方分段列表。
    </div>

    <!-- Segment List with edit controls -->
    <div class="rounded-lg border border-[#1e2535] overflow-hidden">
      <table class="w-full text-xs border-collapse">
        <thead>
          <tr class="bg-[#171b26] text-[#9db0c9]">
            <th class="px-2 py-2 text-center w-8">#</th>
            <th class="px-3 py-2 text-left w-24">时间</th>
            <th class="px-3 py-2 text-left">原文</th>
            <th class="px-2 py-2 text-center w-20">说话人</th>
            <th class="px-2 py-2 text-center w-24">操作</th>
          </tr>
        </thead>
        <tbody>
          <template v-for="(seg, idx) in segments" :key="seg.id">
            <tr
              class="border-t border-[#1e2535] hover:bg-[#1a2030] transition-colors"
              :class="{ 'bg-yellow-900/10': isInSuggestion(seg.id) }"
            >
              <!-- Ordinal -->
              <td class="px-2 py-2.5 text-center text-[#6b7f99]">{{ seg.ordinal }}</td>

              <!-- Time info -->
              <td class="px-3 py-2.5 whitespace-nowrap">
                <div v-if="timingEditId === seg.id" class="flex flex-col gap-1.5 min-w-[160px]">
                  <div class="text-[10px] text-[#9db0c9] mb-0.5">调整时间范围（秒）</div>
                  <div class="flex items-center gap-1.5">
                    <span class="text-[10px] text-[#6b7f99] w-8 shrink-0">起</span>
                    <input
                      v-model.number="editStartSec"
                      type="number"
                      step="0.01"
                      min="0"
                      class="w-20 text-[11px] font-mono bg-[#131720] border border-[#273246] rounded px-1.5 py-0.5 text-[#c5d4e8] focus:outline-none focus:border-blue-500"
                    />
                    <span class="text-[10px] text-[#6b7f99]">s</span>
                  </div>
                  <div class="flex items-center gap-1.5">
                    <span class="text-[10px] text-[#6b7f99] w-8 shrink-0">止</span>
                    <input
                      v-model.number="editEndSec"
                      type="number"
                      step="0.01"
                      :min="editStartSec + 0.1"
                      class="w-20 text-[11px] font-mono bg-[#131720] border border-[#273246] rounded px-1.5 py-0.5 text-[#c5d4e8] focus:outline-none focus:border-blue-500"
                    />
                    <span class="text-[10px] text-[#6b7f99]">s</span>
                  </div>
                  <div class="text-[10px] text-[#6b7f99]">
                    时长 {{ (editEndSec - editStartSec).toFixed(2) }}s
                    <span class="ml-1 text-yellow-500"
                      v-if="Math.abs((editEndSec - editStartSec) - seg.original_duration_ms / 1000) > 0.01">
                      (原 {{ (seg.original_duration_ms / 1000).toFixed(2) }}s)
                    </span>
                  </div>
                  <!-- Audio preview: uses current edit values, not saved DB values -->
                  <audio
                    v-if="timingPreviewId === seg.id"
                    :key="`preview-${seg.id}-${timingPreviewVersion}`"
                    :src="api.originalAudioUrl(jobId, seg.ordinal, Math.round(editStartSec * 1000) * 1000000 + Math.round(editEndSec * 1000), Math.round(editStartSec * 1000), Math.round(editEndSec * 1000))"
                    autoplay
                    class="hidden"
                    @ended="timingPreviewId = null"
                  />
                  <div class="flex gap-1.5 mt-0.5">
                    <button
                      class="px-2 py-0.5 rounded text-[10px] bg-[#1e2535] hover:bg-[#273246] text-[#9db0c9]"
                      @click="replayTimingPreview(seg)"
                    >{{ timingPreviewId === seg.id ? '■ 停止' : '▶ 试听' }}</button>
                    <button
                      class="px-2.5 py-0.5 rounded text-[10px] bg-blue-700 hover:bg-blue-600 text-white font-medium"
                      :disabled="actionLoading['time_' + seg.id] || editEndSec <= editStartSec"
                      @click="saveSegmentTimes(seg)"
                    >{{ actionLoading['time_' + seg.id] ? '保存中…' : '✓ 保存' }}</button>
                    <button
                      class="px-2 py-0.5 rounded text-[10px] bg-[#273246] hover:bg-[#37465f] text-[#9db0c9]"
                      @click="timingEditId = null; timingPreviewId = null"
                    >✕</button>
                  </div>
                </div>
                <template v-else>
                  <div class="text-[10px] text-[#9db0c9] font-mono">
                    {{ formatMs(seg.start_ms) }}
                  </div>
                  <div class="text-[10px] text-[#9db0c9] font-mono">
                    → {{ formatMs(seg.end_ms) }}
                  </div>
                  <div class="text-[10px] text-[#6b7f99]">
                    {{ (seg.original_duration_ms / 1000).toFixed(1) }}s
                    <span
                      v-if="idx + 1 < segments.length"
                      class="ml-1"
                      :class="gapClass(segments[idx+1].start_ms - seg.end_ms)"
                    >
                      +{{ Math.round(segments[idx+1].start_ms - seg.end_ms) }}ms
                    </span>
                  </div>
                  <button
                    class="mt-0.5 px-1.5 py-0 rounded text-[9px] bg-[#1e2535] hover:bg-[#273246] text-[#6b7f99] hover:text-[#9db0c9]"
                    @click="openTimingEdit(seg)"
                  >✏ 调整</button>
                </template>
              </td>

              <!-- Source text -->
              <td class="px-3 py-2.5">
                <div v-if="editingId === seg.id" class="flex flex-col gap-1.5">
                  <div class="text-[10px] text-[#9db0c9] mb-1">
                    拆分字符位置（点击文字选择位置）：
                  </div>
                  <div
                    class="font-mono text-[#c5d4e8] leading-relaxed cursor-text select-text whitespace-pre-wrap bg-[#131720] rounded px-2 py-1.5 border border-[#273246]"
                    @click="onTextClick($event, seg)"
                  >
                    <span
                      v-for="(char, ci) in Array.from(seg.src_text)"
                      :key="ci"
                      :class="{ 'bg-blue-600/50 rounded': ci === splitCharIndex }"
                    >{{ char }}</span>
                  </div>
                  <div class="flex items-center gap-2">
                    <span class="text-[#6b7f99]">位置 {{ splitCharIndex }} / {{ seg.src_text.length }}</span>
                    <input
                      v-model.number="splitCharIndex"
                      type="range"
                      :min="1"
                      :max="seg.src_text.length - 1"
                      class="flex-1"
                    />
                  </div>
                  <div class="flex gap-1.5 mt-1">
                    <button
                      class="px-2.5 py-1 rounded bg-orange-700 hover:bg-orange-600 text-white text-[11px] font-medium"
                      :disabled="actionLoading['split_' + seg.id]"
                      @click="doSplit(seg)"
                    >
                      {{ actionLoading['split_' + seg.id] ? '拆分中…' : '确认拆分' }}
                    </button>
                    <button
                      class="px-2.5 py-1 rounded bg-[#273246] hover:bg-[#37465f] text-[#9db0c9] text-[11px]"
                      @click="editingId = null"
                    >
                      取消
                    </button>
                  </div>
                </div>
                <div v-else-if="transcriptEditId === seg.id" class="flex flex-col gap-1.5">
                  <div class="text-[10px] text-[#9db0c9] mb-0.5">编辑 ASR 原文（不影响时间轴）：</div>
                  <textarea
                    v-model="transcriptDraft"
                    rows="3"
                    class="w-full bg-[#131720] border border-[#273246] rounded px-2 py-1.5 text-xs text-[#c5d4e8] focus:outline-none focus:border-blue-500 leading-relaxed resize-y"
                  />
                  <div class="flex items-center gap-2 text-[10px]">
                    <span :class="transcriptDraftBytes > 8192 ? 'text-red-400' : 'text-[#6b7f99]'">
                      {{ transcriptDraftBytes }} / 8192 字节
                    </span>
                    <span class="ml-auto flex gap-1.5">
                      <button
                        class="px-2.5 py-1 rounded bg-blue-700 hover:bg-blue-600 text-white text-[11px] font-medium disabled:opacity-50"
                        :disabled="actionLoading['asr_' + seg.id] || !transcriptDraft.trim() || transcriptDraftBytes > 8192"
                        @click="saveTranscript(seg)"
                      >
                        {{ actionLoading['asr_' + seg.id] ? '保存中…' : '✓ 保存' }}
                      </button>
                      <button
                        class="px-2 py-1 rounded bg-[#273246] hover:bg-[#37465f] text-[#9db0c9] text-[11px]"
                        @click="cancelTranscriptEdit"
                      >✕</button>
                    </span>
                  </div>
                </div>
                <div v-else class="flex flex-col gap-1">
                  <p class="text-[#c5d4e8] leading-relaxed">{{ seg.src_text }}</p>
                  <button
                    class="self-start px-1.5 py-0 rounded text-[9px] bg-[#1e2535] hover:bg-[#273246] text-[#6b7f99] hover:text-[#9db0c9]"
                    @click="openTranscriptEdit(seg)"
                  >✏ 编辑原文</button>
                </div>
              </td>

              <!-- Speaker -->
              <td class="px-2 py-2.5 text-center text-[#9db0c9]">
                <span class="px-1.5 py-0.5 rounded bg-[#1e2535] font-mono text-[10px]">
                  {{ seg.speaker_label }}
                </span>
              </td>

              <!-- Actions -->
              <td class="px-2 py-2.5 text-center">
                <div class="flex flex-col gap-1 items-center">
                  <!-- Original audio -->
                  <audio
                    v-if="playingId === seg.id"
                    :src="api.originalAudioUrl(jobId, seg.ordinal, seg.start_ms * 1000000 + seg.end_ms)"
                    autoplay
                    class="hidden"
                    @ended="playingId = null"
                  />
                  <button
                    class="px-2 py-0.5 rounded text-[10px] bg-[#1e2535] hover:bg-[#273246] text-[#9db0c9]"
                    @click="playingId = playingId === seg.id ? null : seg.id"
                  >
                    {{ playingId === seg.id ? '■' : '▶' }} 原声
                  </button>

                  <!-- Merge with next -->
                  <button
                    v-if="idx + 1 < segments.length"
                    class="px-2 py-0.5 rounded text-[10px] bg-[#1e2535] hover:bg-blue-700/40 text-[#9db0c9] hover:text-blue-300"
                    :disabled="actionLoading['merge_' + seg.id]"
                    @click="mergeWithNext(seg, segments[idx+1])"
                  >
                    {{ actionLoading['merge_' + seg.id] ? '…' : '⊕ 合并↓' }}
                  </button>

                  <!-- Split -->
                  <button
                    class="px-2 py-0.5 rounded text-[10px] bg-[#1e2535] hover:bg-orange-700/40 text-[#9db0c9] hover:text-orange-300"
                    @click="openSplit(seg)"
                  >
                    ✂ 拆分
                  </button>

                  <!-- Per-segment ASR re-transcription (Wave 2) -->
                  <button
                    class="px-2 py-0.5 rounded text-[10px] bg-[#1e2535] hover:bg-blue-700/40 text-[#9db0c9] hover:text-blue-300 disabled:opacity-50"
                    :disabled="actionLoading['asr_' + seg.id] || !!retrySegmentLoadingId"
                    @click="doRetrySegmentASR(seg)"
                  >
                    {{ retrySegmentLoadingId === seg.id ? '识别中…' : '↻ 重新识别' }}
                  </button>
                </div>
              </td>
            </tr>
          </template>
        </tbody>
      </table>
    </div>

    <!-- Error message -->
    <div v-if="errorMsg" class="rounded-lg bg-red-900/20 border border-red-700/40 px-4 py-2 text-xs text-red-400">
      {{ errorMsg }}
    </div>

    <!-- Bottom action bar -->
    <div class="sticky bottom-0 z-10 flex items-center gap-3 px-4 py-3 rounded-xl border border-[#273246] bg-[#0f1520]/95 backdrop-blur">
      <div class="flex-1 text-xs text-[#9db0c9]">
        共 <span class="text-white font-medium">{{ segments.length }}</span> 段
        <span v-if="pendingSuggestions.length > 0" class="ml-2 text-yellow-400">
          · {{ pendingSuggestions.length }} 条建议待处理
        </span>
      </div>
      <button
        class="px-3 py-1.5 rounded bg-[#273246] hover:bg-[#37465f] text-[#9db0c9] text-xs font-medium disabled:opacity-50"
        :disabled="confirmLoading || retryLoading"
        @click="doRetryASR"
      >
        {{ retryLoading ? '重置中…' : '↻ 重试 ASR 分段' }}
      </button>
      <button
        class="px-4 py-1.5 rounded bg-blue-600 hover:bg-blue-500 text-white text-xs font-semibold disabled:opacity-50"
        :disabled="confirmLoading || retryLoading"
        @click="doConfirm"
      >
        {{ confirmLoading ? '提交中…' : '✓ 确认分段，开始翻译' }}
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch, nextTick } from 'vue'
import { api, type Segment, type SegmentSuggestion } from '../api'

const props = defineProps<{
  jobId: number
}>()

const emit = defineEmits<{
  (e: 'confirmed'): void
  (e: 'asr-retried'): void
  (e: 'segments-changed'): void
}>()

const segments = ref<Segment[]>([])
const suggestions = ref<SegmentSuggestion[]>([])
const suggestionsLoading = ref(false)
const confirmLoading = ref(false)
const retryLoading = ref(false)
const errorMsg = ref('')
const actionLoading = ref<Record<string, boolean>>({})
const editingId = ref<number | null>(null)
const splitCharIndex = ref(0)
const playingId = ref<number | null>(null)
const timingEditId = ref<number | null>(null)
const timingPreviewId = ref<number | null>(null)
const timingPreviewVersion = ref(0)  // incremented on each explicit "试听" click
const editStartSec = ref(0)
const editEndSec = ref(0)
// transcriptEditId / transcriptDraft drive the inline ASR-text editor.  They
// are mutually exclusive with editingId (split) and timingEditId so the UI
// never has two pending edits competing for the same row.
const transcriptEditId = ref<number | null>(null)
const transcriptDraft = ref('')
const transcriptDraftBytes = computed(() =>
  new TextEncoder().encode(transcriptDraft.value).length
)
// retrySegmentLoadingId holds the id of the segment currently waiting for
// ml-service to re-run ASR.  At most one re-transcription runs at a time so
// the GPU is never queued behind multiple concurrent requests; other rows'
// "↻ 重新识别" buttons are disabled while this is non-null.
const retrySegmentLoadingId = ref<number | null>(null)

const segmentMap = computed<Record<number, Segment>>(() => {
  const m: Record<number, Segment> = {}
  for (const s of segments.value) m[s.id] = s
  return m
})

const pendingSuggestions = computed(() =>
  suggestions.value.filter(s => s.status === 'pending')
)

const suggestionSegmentIds = computed(() => {
  const ids = new Set<number>()
  for (const sug of suggestions.value) {
    if (sug.status === 'pending') {
      for (const id of sug.segment_ids) ids.add(id)
    }
  }
  return ids
})

function isInSuggestion(id: number) {
  return suggestionSegmentIds.value.has(id)
}

function getSegmentOrdinals(ids: number[]): number[] {
  return ids.map(id => segmentMap.value[id]?.ordinal ?? id)
}

function formatMs(ms: number): string {
  const totalSec = Math.floor(ms / 1000)
  const m = Math.floor(totalSec / 60)
  const s = totalSec % 60
  const frac = Math.floor((ms % 1000) / 10)
  return `${m}:${String(s).padStart(2, '0')}.${String(frac).padStart(2, '0')}`
}

function gapClass(gapMs: number): string {
  if (gapMs < 200) return 'text-red-400'
  if (gapMs < 500) return 'text-yellow-400'
  return 'text-green-500'
}

function confidenceClass(conf: number): string {
  if (conf >= 0.8) return 'bg-green-800/40 text-green-400'
  if (conf >= 0.5) return 'bg-yellow-800/40 text-yellow-400'
  return 'bg-[#1e2535] text-[#9db0c9]'
}

function truncate(text: string, len: number): string {
  return text.length > len ? text.slice(0, len) + '…' : text
}

async function loadData() {
  suggestionsLoading.value = true
  errorMsg.value = ''
  try {
    const [segsRes, sugsRes] = await Promise.all([
      api.listSegments(props.jobId),
      api.listSegmentSuggestions(props.jobId),
    ])
    segments.value = segsRes.segments
    suggestions.value = sugsRes.suggestions
  } catch (e: unknown) {
    errorMsg.value = (e as Error).message
  } finally {
    suggestionsLoading.value = false
  }
}

async function acceptSuggestion(sug: SegmentSuggestion) {
  actionLoading.value[sug.id] = true
  errorMsg.value = ''
  try {
    await api.acceptSuggestion(props.jobId, sug.id)
    // Reload to reflect merge
    await loadData()
    emit('segments-changed')
  } catch (e: unknown) {
    errorMsg.value = (e as Error).message
  } finally {
    actionLoading.value[sug.id] = false
  }
}

async function rejectSuggestion(sug: SegmentSuggestion) {
  actionLoading.value[sug.id] = true
  try {
    await api.rejectSuggestion(props.jobId, sug.id)
    sug.status = 'rejected'
  } catch (e: unknown) {
    errorMsg.value = (e as Error).message
  } finally {
    actionLoading.value[sug.id] = false
  }
}

async function mergeWithNext(a: Segment, b: Segment) {
  const key = 'merge_' + a.id
  actionLoading.value[key] = true
  errorMsg.value = ''
  try {
    await api.mergeSegments(props.jobId, [a.id, b.id])
    await loadData()
    emit('segments-changed')
  } catch (e: unknown) {
    errorMsg.value = (e as Error).message
  } finally {
    actionLoading.value[key] = false
  }
}

function openSplit(seg: Segment) {
  if (editingId.value === seg.id) {
    editingId.value = null
    return
  }
  // Close other inline editors so the row only has one mutating control open.
  transcriptEditId.value = null
  timingEditId.value = null
  editingId.value = seg.id
  splitCharIndex.value = Math.floor(seg.src_text.length / 2)
}

function onTextClick(event: MouseEvent, seg: Segment) {
  const target = event.target as HTMLElement
  if (!target || !target.dataset) return
  const parent = target.parentElement
  if (!parent) return
  const spans = Array.from(parent.children)
  const idx = spans.indexOf(target)
  if (idx >= 0) splitCharIndex.value = idx
}

async function doSplit(seg: Segment) {
  const key = 'split_' + seg.id
  actionLoading.value[key] = true
  errorMsg.value = ''
  try {
    await api.splitSegment(props.jobId, seg.id, splitCharIndex.value)
    editingId.value = null
    await loadData()
    emit('segments-changed')
  } catch (e: unknown) {
    errorMsg.value = (e as Error).message
  } finally {
    actionLoading.value[key] = false
  }
}

function openTimingEdit(seg: Segment) {
  // Close other inline editors so the row only has one mutating control open.
  editingId.value = null
  transcriptEditId.value = null
  timingEditId.value = seg.id
  timingPreviewId.value = null
  editStartSec.value = Math.round(seg.start_ms) / 1000
  editEndSec.value = Math.round(seg.end_ms) / 1000
}

function openTranscriptEdit(seg: Segment) {
  if (transcriptEditId.value === seg.id) {
    transcriptEditId.value = null
    return
  }
  // Close other inline editors so the row only has one mutating control open.
  editingId.value = null
  timingEditId.value = null
  transcriptEditId.value = seg.id
  transcriptDraft.value = seg.src_text
}

function cancelTranscriptEdit() {
  transcriptEditId.value = null
  transcriptDraft.value = ''
}

async function saveTranscript(seg: Segment) {
  const trimmed = transcriptDraft.value.trim()
  if (!trimmed) {
    errorMsg.value = '原文不能为空'
    return
  }
  if (new TextEncoder().encode(trimmed).length > 8192) {
    errorMsg.value = '原文超过 8KB 上限'
    return
  }
  if (trimmed === seg.src_text) {
    transcriptEditId.value = null
    return
  }
  const key = 'asr_' + seg.id
  actionLoading.value[key] = true
  errorMsg.value = ''
  try {
    await api.patchSegmentSrcText(props.jobId, seg.id, trimmed)
    transcriptEditId.value = null
    await loadData()
    emit('segments-changed')
  } catch (e: unknown) {
    errorMsg.value = (e as Error).message
  } finally {
    actionLoading.value[key] = false
  }
}

async function saveSegmentTimes(seg: Segment) {
  const key = 'time_' + seg.id
  actionLoading.value[key] = true
  errorMsg.value = ''
  try {
    const startMs = Math.round(editStartSec.value * 1000)
    const endMs = Math.round(editEndSec.value * 1000)
    await api.patchSegmentTimes(props.jobId, seg.id, startMs, endMs)
    timingEditId.value = null
    timingPreviewId.value = null
    await loadData()
    emit('segments-changed')
  } catch (e: unknown) {
    errorMsg.value = (e as Error).message
  } finally {
    actionLoading.value[key] = false
  }
}

// Force audio element to re-mount so it re-fetches with current edit values.
// Using an explicit version counter prevents auto-replay on every input keystroke.
function replayTimingPreview(seg: Segment) {
  if (timingPreviewId.value === seg.id) {
    timingPreviewId.value = null
    return
  }
  timingPreviewId.value = null
  timingPreviewVersion.value++
  nextTick(() => { timingPreviewId.value = seg.id })
}

async function doConfirm() {
  confirmLoading.value = true
  errorMsg.value = ''
  try {
    await api.confirmSegmentation(props.jobId)
    emit('confirmed')
  } catch (e: unknown) {
    errorMsg.value = (e as Error).message
  } finally {
    confirmLoading.value = false
  }
}

async function doRetryASR() {
  if (!confirm('重试 ASR 分段将清空当前所有分段和建议，确定继续？')) return
  retryLoading.value = true
  errorMsg.value = ''
  try {
    await api.retryASR(props.jobId)
    emit('asr-retried')
  } catch (e: unknown) {
    errorMsg.value = (e as Error).message
  } finally {
    retryLoading.value = false
  }
}

async function doRetrySegmentASR(seg: Segment) {
  if (retrySegmentLoadingId.value !== null) return
  if (!confirm(`重新识别第 ${seg.ordinal + 1} 段语音将覆盖当前原文，确定继续？`)) return
  // Discard any in-flight inline edits for this row so the new ASR result
  // is the authoritative source after the request finishes.
  if (transcriptEditId.value === seg.id) {
    transcriptEditId.value = null
    transcriptDraft.value = ''
  }
  if (editingId.value === seg.id) editingId.value = null
  if (timingEditId.value === seg.id) timingEditId.value = null

  retrySegmentLoadingId.value = seg.id
  errorMsg.value = ''
  try {
    const resp = await api.retrySegmentASR(props.jobId, seg.id)
    if (resp.updated && resp.src_text) {
      // Patch the local row in place so the user sees the new transcript
      // without waiting for a full reload.
      seg.src_text = resp.src_text
    } else if (resp.warning === 'empty_transcription') {
      errorMsg.value = resp.message ||
        'ASR 未识别到文本，请使用 ✏ 编辑原文 手动输入。'
    }
  } catch (e: unknown) {
    errorMsg.value = (e as Error).message
  } finally {
    retrySegmentLoadingId.value = null
  }
}

onMounted(loadData)
watch(() => props.jobId, loadData)
</script>
