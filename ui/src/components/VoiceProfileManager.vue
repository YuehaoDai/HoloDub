<template>
  <div class="p-4 space-y-6">
    <!-- 音色列表 -->
    <div>
      <div class="flex items-center justify-between mb-3">
        <h3 class="text-sm font-medium text-white">音色档案</h3>
        <button
          class="text-xs px-3 py-1.5 rounded bg-blue-600 hover:bg-blue-500 text-white transition-colors"
          @click="showCreate = !showCreate"
        >
          {{ showCreate ? '收起' : '+ 新建音色' }}
        </button>
      </div>

      <!-- 新建表单 -->
      <div v-if="showCreate" class="mb-4 p-3 rounded-lg bg-[#1e2535] border border-[#273246] space-y-3">
        <div class="text-xs font-medium text-[#9db0c9]">新建音色档案</div>
        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="block text-[10px] text-[#9db0c9] mb-1">名称 *</label>
            <input
              v-model="form.name"
              class="w-full text-xs px-2 py-1.5 rounded bg-[#111722] border border-[#273246] text-[#f2f5f7] focus:outline-none focus:border-[#4a6080]"
              placeholder="e.g. 主角女声"
            />
          </div>
          <div>
            <label class="block text-[10px] text-[#9db0c9] mb-1">模式</label>
            <select
              v-model="form.mode"
              class="w-full text-xs px-2 py-1.5 rounded bg-[#111722] border border-[#273246] text-[#f2f5f7] focus:outline-none"
            >
              <option value="">— 默认 —</option>
              <option value="clone">clone（音色克隆）</option>
              <option value="pretrained">pretrained（预训练）</option>
              <option value="edge_tts">edge_tts（Edge TTS）</option>
            </select>
          </div>
          <div>
            <label class="block text-[10px] text-[#9db0c9] mb-1">语言</label>
            <input
              v-model="form.language"
              class="w-full text-xs px-2 py-1.5 rounded bg-[#111722] border border-[#273246] text-[#f2f5f7] focus:outline-none focus:border-[#4a6080]"
              placeholder="zh / ja / en"
            />
          </div>
          <div>
            <label class="block text-[10px] text-[#9db0c9] mb-1">Provider</label>
            <input
              v-model="form.provider"
              class="w-full text-xs px-2 py-1.5 rounded bg-[#111722] border border-[#273246] text-[#f2f5f7] focus:outline-none focus:border-[#4a6080]"
              placeholder="indextts2 / edge_tts"
            />
          </div>
        </div>

        <!-- 参考音频路径 -->
        <div>
          <label class="block text-[10px] text-[#9db0c9] mb-1">参考音频路径（relpath，一行一个）</label>
          <textarea
            v-model="samplePathsText"
            rows="3"
            class="w-full text-xs px-2 py-1.5 rounded bg-[#111722] border border-[#273246] text-[#f2f5f7] placeholder-[#37465f] focus:outline-none focus:border-[#4a6080] resize-none font-mono"
            placeholder="voices/spk01/ref.wav"
          ></textarea>
        </div>

        <!-- 高级字段（折叠） -->
        <details class="text-[10px] text-[#9db0c9]">
          <summary class="cursor-pointer select-none hover:text-white">高级字段（checkpoint / edge_tts 声音名）</summary>
          <div class="mt-2 space-y-2">
            <div>
              <label class="block mb-1">Checkpoint relpath</label>
              <input v-model="form.checkpoint_relpath" class="w-full px-2 py-1 rounded bg-[#111722] border border-[#273246] text-[#f2f5f7] focus:outline-none" placeholder="voices/spk01/model.pth" />
            </div>
            <div>
              <label class="block mb-1">Index relpath</label>
              <input v-model="form.index_relpath" class="w-full px-2 py-1 rounded bg-[#111722] border border-[#273246] text-[#f2f5f7] focus:outline-none" placeholder="voices/spk01/index.bin" />
            </div>
            <div>
              <label class="block mb-1">Config relpath</label>
              <input v-model="form.config_relpath" class="w-full px-2 py-1 rounded bg-[#111722] border border-[#273246] text-[#f2f5f7] focus:outline-none" placeholder="voices/spk01/config.yaml" />
            </div>
            <div>
              <label class="block mb-1">Edge TTS 声音名（meta.edge_tts_voice）</label>
              <input v-model="form.edge_tts_voice" class="w-full px-2 py-1 rounded bg-[#111722] border border-[#273246] text-[#f2f5f7] focus:outline-none" placeholder="zh-CN-XiaoxiaoNeural" />
            </div>
          </div>
        </details>

        <div class="flex gap-2">
          <button
            class="text-xs px-3 py-1.5 rounded bg-blue-600 hover:bg-blue-500 text-white transition-colors disabled:opacity-50"
            :disabled="creating || !form.name.trim()"
            @click="createProfile"
          >
            {{ creating ? '创建中...' : '创建' }}
          </button>
          <button
            class="text-xs px-3 py-1.5 rounded bg-transparent text-[#9db0c9] hover:text-white transition-colors"
            @click="resetForm"
          >
            重置
          </button>
        </div>
        <div v-if="createError" class="text-xs text-red-400">{{ createError }}</div>
      </div>

      <!-- 音色列表 -->
      <div v-if="loading" class="text-xs text-[#9db0c9]">加载中...</div>
      <div v-else-if="!profiles.length" class="text-xs text-[#37465f] py-4 text-center">暂无音色档案</div>
      <div v-else class="space-y-2">
        <div
          v-for="vp in profiles"
          :key="vp.id"
          class="bg-[#1e2535] border border-[#273246] rounded-lg px-4 py-3"
        >
          <div class="flex items-start justify-between gap-3">
            <div class="flex-1 min-w-0">
              <div class="flex items-center gap-2 mb-1">
                <span class="text-xs font-medium text-white">{{ vp.name }}</span>
                <span class="text-[10px] px-1.5 py-0.5 rounded bg-[#273246] text-[#9db0c9]">{{ vp.mode || '—' }}</span>
                <span class="text-[10px] px-1.5 py-0.5 rounded bg-[#273246] text-[#9db0c9]">{{ vp.language || '—' }}</span>
                <span
                  v-if="vp.validation_status"
                  class="text-[10px] px-1.5 py-0.5 rounded"
                  :class="vp.validation_status === 'valid' ? 'bg-green-900/60 text-green-300' : 'bg-red-900/60 text-red-300'"
                >
                  {{ vp.validation_status === 'valid' ? '✓ 有效' : '✗ 无效' }}
                </span>
              </div>
              <div v-if="vp.provider" class="text-[10px] text-[#37465f]">provider: {{ vp.provider }}</div>
              <div v-if="vp.validation_error" class="text-[10px] text-red-400 mt-1 truncate" :title="vp.validation_error">
                {{ vp.validation_error }}
              </div>
            </div>
            <div class="flex gap-1 shrink-0">
              <button
                class="text-[10px] px-2 py-1 rounded bg-[#273246] hover:bg-[#37465f] text-[#9db0c9] hover:text-white transition-colors disabled:opacity-50"
                :disabled="validating[vp.id]"
                @click="validateProfile(vp.id)"
              >
                {{ validating[vp.id] ? '…' : '验证' }}
              </button>
            </div>
          </div>
          <!-- 参考音频列表 -->
          <div v-if="parseSamplePaths(vp).length" class="mt-2 space-y-0.5">
            <div
              v-for="(path, idx) in parseSamplePaths(vp)"
              :key="idx"
              class="flex items-center gap-2"
            >
              <span class="text-[10px] text-[#37465f] font-mono truncate flex-1">{{ path }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue";
import { api, type VoiceProfile } from "../api";

const profiles = ref<VoiceProfile[]>([]);
const loading = ref(false);
const showCreate = ref(false);
const creating = ref(false);
const createError = ref("");
const validating = ref<Record<number, boolean>>({});
const samplePathsText = ref("");

const form = ref({
  name: "",
  mode: "",
  provider: "",
  language: "",
  checkpoint_relpath: "",
  index_relpath: "",
  config_relpath: "",
  edge_tts_voice: "",
});

async function load() {
  loading.value = true;
  try {
    const res = await api.listVoiceProfiles();
    profiles.value = res.voice_profiles || [];
  } finally {
    loading.value = false;
  }
}

function parseSamplePaths(vp: VoiceProfile): string[] {
  if (!vp.sample_relpaths) return [];
  if (Array.isArray(vp.sample_relpaths)) return vp.sample_relpaths;
  try { return JSON.parse(vp.sample_relpaths as unknown as string) as string[]; } catch { return []; }
}

async function createProfile() {
  creating.value = true;
  createError.value = "";
  try {
    const samplePaths = samplePathsText.value
      .split("\n")
      .map((l) => l.trim())
      .filter(Boolean);
    const meta: Record<string, unknown> = {};
    if (form.value.edge_tts_voice.trim()) {
      meta["edge_tts_voice"] = form.value.edge_tts_voice.trim();
    }
    await api.createVoiceProfile({
      name: form.value.name.trim(),
      mode: form.value.mode || undefined,
      provider: form.value.provider || undefined,
      language: form.value.language || undefined,
      sample_relpaths: samplePaths.length ? samplePaths : undefined,
      checkpoint_relpath: form.value.checkpoint_relpath || undefined,
      index_relpath: form.value.index_relpath || undefined,
      config_relpath: form.value.config_relpath || undefined,
      meta: Object.keys(meta).length ? meta : undefined,
    });
    resetForm();
    showCreate.value = false;
    await load();
  } catch (e: unknown) {
    createError.value = e instanceof Error ? e.message : String(e);
  } finally {
    creating.value = false;
  }
}

async function validateProfile(id: number) {
  validating.value[id] = true;
  try {
    await api.validateVoiceProfile(id);
    await load();
  } catch (e: unknown) {
    alert(e instanceof Error ? e.message : String(e));
  } finally {
    validating.value[id] = false;
  }
}

function resetForm() {
  form.value = { name: "", mode: "", provider: "", language: "", checkpoint_relpath: "", index_relpath: "", config_relpath: "", edge_tts_voice: "" };
  samplePathsText.value = "";
  createError.value = "";
}

onMounted(load);
</script>
