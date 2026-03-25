<template>
  <div class="p-4 space-y-6">
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
        <ProfileFormFields v-model="form" v-model:sample-paths-text="samplePathsText" @browse-audio="openAudioPicker('create')" />
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
            @click="resetCreateForm"
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
          class="bg-[#1e2535] border rounded-lg overflow-hidden transition-colors"
          :class="editingId === vp.id ? 'border-blue-500/50' : 'border-[#273246]'"
        >
          <!-- 卡片头部 -->
          <div class="flex items-start justify-between gap-3 px-4 py-3">
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
                class="text-[10px] px-2 py-1 rounded transition-colors"
                :class="editingId === vp.id
                  ? 'bg-blue-700 text-white hover:bg-blue-600'
                  : 'bg-[#273246] hover:bg-[#37465f] text-[#9db0c9] hover:text-white'"
                @click="toggleEdit(vp)"
              >
                {{ editingId === vp.id ? '取消' : '编辑' }}
              </button>
              <button
                class="text-[10px] px-2 py-1 rounded bg-[#273246] hover:bg-[#37465f] text-[#9db0c9] hover:text-white transition-colors disabled:opacity-50"
                :disabled="validating[vp.id]"
                @click="validateProfile(vp.id)"
              >
                {{ validating[vp.id] ? '…' : '验证' }}
              </button>
              <button
                class="text-[10px] px-2 py-1 rounded bg-[#273246] hover:bg-red-900/60 text-[#9db0c9] hover:text-red-300 transition-colors disabled:opacity-50"
                :disabled="deleting[vp.id]"
                @click="deleteProfile(vp)"
              >
                {{ deleting[vp.id] ? '…' : '删除' }}
              </button>
            </div>
          </div>

          <!-- 参考音频列表（非编辑状态） -->
          <div v-if="editingId !== vp.id && parseSamplePaths(vp).length" class="px-4 pb-3 space-y-0.5">
            <div v-for="(path, idx) in parseSamplePaths(vp)" :key="idx" class="flex items-center gap-2">
              <span class="text-[10px] text-[#37465f] font-mono truncate flex-1">{{ path }}</span>
            </div>
          </div>

          <!-- 编辑表单（行内展开） -->
          <div v-if="editingId === vp.id" class="px-4 pb-4 pt-1 border-t border-blue-500/20 space-y-3">
            <ProfileFormFields v-model="editForm" v-model:sample-paths-text="editSamplePathsText" @browse-audio="openAudioPicker('edit')" />
            <div class="flex gap-2">
              <button
                class="text-xs px-3 py-1.5 rounded bg-blue-600 hover:bg-blue-500 text-white transition-colors disabled:opacity-50"
                :disabled="saving || !editForm.name.trim()"
                @click="saveEdit(vp.id)"
              >
                {{ saving ? '保存中...' : '保存' }}
              </button>
              <button
                class="text-xs px-3 py-1.5 rounded bg-transparent text-[#9db0c9] hover:text-white transition-colors"
                @click="cancelEdit"
              >
                取消
              </button>
            </div>
            <div v-if="editError" class="text-xs text-red-400">{{ editError }}</div>
          </div>
        </div>
      </div>
    </div>

    <!-- 音频文件浏览弹窗 -->
    <FilePicker
      v-model="showAudioPicker"
      filter="audio"
      :multiple="true"
      @select-multiple="onAudioFilesSelected"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from "vue";
import { api, type VoiceProfile } from "../api";
import FilePicker from "./FilePicker.vue";

// ─── 子组件：共用表单字段 ──────────────────────────────────────────────
const ProfileFormFields = {
  name: "ProfileFormFields",
  props: {
    modelValue: { type: Object, required: true },
    samplePathsText: { type: String, default: "" },
  },
  emits: ["update:modelValue", "update:samplePathsText", "browse-audio"],
  template: `
    <div class="space-y-3">
      <div class="grid grid-cols-2 gap-3">
        <div>
          <label class="block text-[10px] text-[#9db0c9] mb-1">名称 *</label>
          <input
            class="w-full text-xs px-2 py-1.5 rounded bg-[#111722] border border-[#273246] text-[#f2f5f7] focus:outline-none focus:border-[#4a6080]"
            :value="modelValue.name"
            placeholder="e.g. 主角女声"
            @input="$emit('update:modelValue', { ...modelValue, name: $event.target.value })"
          />
        </div>
        <div>
          <label class="block text-[10px] text-[#9db0c9] mb-1">模式</label>
          <select
            class="w-full text-xs px-2 py-1.5 rounded bg-[#111722] border border-[#273246] text-[#f2f5f7] focus:outline-none"
            :value="modelValue.mode"
            @change="$emit('update:modelValue', { ...modelValue, mode: $event.target.value })"
          >
            <option value="">— 默认 —</option>
            <option value="clone">clone（音色克隆）</option>
            <option value="pretrained">pretrained（预训练）</option>
          </select>
        </div>
        <div>
          <label class="block text-[10px] text-[#9db0c9] mb-1">语言</label>
          <input
            class="w-full text-xs px-2 py-1.5 rounded bg-[#111722] border border-[#273246] text-[#f2f5f7] focus:outline-none focus:border-[#4a6080]"
            :value="modelValue.language"
            placeholder="zh / ja / en"
            @input="$emit('update:modelValue', { ...modelValue, language: $event.target.value })"
          />
        </div>
        <div>
          <label class="block text-[10px] text-[#9db0c9] mb-1">Provider</label>
          <input
            class="w-full text-xs px-2 py-1.5 rounded bg-[#111722] border border-[#273246] text-[#f2f5f7] focus:outline-none focus:border-[#4a6080]"
            :value="modelValue.provider"
            placeholder="indextts2"
            @input="$emit('update:modelValue', { ...modelValue, provider: $event.target.value })"
          />
        </div>
      </div>
      <div>
        <div class="flex items-center justify-between mb-1">
          <label class="text-[10px] text-[#9db0c9]">参考音频路径（一行一个）</label>
          <button
            type="button"
            class="text-[10px] px-2 py-0.5 rounded bg-[#273246] hover:bg-[#37465f] text-[#9db0c9] hover:text-white transition-colors"
            @click="$emit('browse-audio')"
          >+ 浏览</button>
        </div>
        <textarea
          class="w-full text-xs px-2 py-1.5 rounded bg-[#111722] border border-[#273246] text-[#f2f5f7] placeholder-[#37465f] focus:outline-none focus:border-[#4a6080] resize-none font-mono"
          rows="3"
          :value="samplePathsText"
          placeholder="voices/spk01/ref.wav"
          @input="$emit('update:samplePathsText', $event.target.value)"
        ></textarea>
        <p class="text-[10px] text-[#37465f] mt-1">相对 DATA_ROOT 的路径，不要以 / 开头</p>
      </div>
      <details class="text-[10px] text-[#9db0c9]">
        <summary class="cursor-pointer select-none hover:text-white">高级字段（checkpoint / index / config）</summary>
        <div class="mt-2 space-y-2">
          <div>
            <label class="block mb-1">Checkpoint relpath</label>
            <input class="w-full px-2 py-1 rounded bg-[#111722] border border-[#273246] text-[#f2f5f7] focus:outline-none" :value="modelValue.checkpoint_relpath" placeholder="voices/spk01/model.pth" @input="$emit('update:modelValue', { ...modelValue, checkpoint_relpath: $event.target.value })" />
          </div>
          <div>
            <label class="block mb-1">Index relpath</label>
            <input class="w-full px-2 py-1 rounded bg-[#111722] border border-[#273246] text-[#f2f5f7] focus:outline-none" :value="modelValue.index_relpath" placeholder="voices/spk01/index.bin" @input="$emit('update:modelValue', { ...modelValue, index_relpath: $event.target.value })" />
          </div>
          <div>
            <label class="block mb-1">Config relpath</label>
            <input class="w-full px-2 py-1 rounded bg-[#111722] border border-[#273246] text-[#f2f5f7] focus:outline-none" :value="modelValue.config_relpath" placeholder="voices/spk01/config.yaml" @input="$emit('update:modelValue', { ...modelValue, config_relpath: $event.target.value })" />
          </div>
        </div>
      </details>
    </div>
  `,
};

// ─── 主逻辑 ──────────────────────────────────────────────────────────────
const profiles = ref<VoiceProfile[]>([]);
const loading = ref(false);
const showCreate = ref(false);
const creating = ref(false);
const createError = ref("");
const validating = ref<Record<number, boolean>>({});
const deleting = ref<Record<number, boolean>>({});

const samplePathsText = ref("");
const form = ref(emptyForm());

const editingId = ref<number | null>(null);
const editSamplePathsText = ref("");
const editForm = ref(emptyForm());
const saving = ref(false);
const editError = ref("");

// FilePicker state
const showAudioPicker = ref(false);
let audioPickerTarget: "create" | "edit" = "create";

function emptyForm() {
  return { name: "", mode: "", provider: "", language: "", checkpoint_relpath: "", index_relpath: "", config_relpath: "" };
}

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

function samplePathsToList(text: string): string[] {
  return text.split("\n").map((l) => l.trim().replace(/^\/+/, "")).filter(Boolean);
}

function openAudioPicker(target: "create" | "edit") {
  audioPickerTarget = target;
  showAudioPicker.value = true;
}

function onAudioFilesSelected(paths: string[]) {
  if (audioPickerTarget === "create") {
    const existing = samplePathsToList(samplePathsText.value);
    const merged = [...new Set([...existing, ...paths])];
    samplePathsText.value = merged.join("\n");
  } else {
    const existing = samplePathsToList(editSamplePathsText.value);
    const merged = [...new Set([...existing, ...paths])];
    editSamplePathsText.value = merged.join("\n");
  }
}

async function createProfile() {
  creating.value = true;
  createError.value = "";
  try {
    const samplePaths = samplePathsToList(samplePathsText.value);
    await api.createVoiceProfile({
      name: form.value.name.trim(),
      mode: form.value.mode || undefined,
      provider: form.value.provider || undefined,
      language: form.value.language || undefined,
      sample_relpaths: samplePaths.length ? samplePaths : undefined,
      checkpoint_relpath: form.value.checkpoint_relpath || undefined,
      index_relpath: form.value.index_relpath || undefined,
      config_relpath: form.value.config_relpath || undefined,
    });
    resetCreateForm();
    showCreate.value = false;
    await load();
  } catch (e: unknown) {
    createError.value = e instanceof Error ? e.message : String(e);
  } finally {
    creating.value = false;
  }
}

function resetCreateForm() {
  form.value = emptyForm();
  samplePathsText.value = "";
  createError.value = "";
}

function toggleEdit(vp: VoiceProfile) {
  if (editingId.value === vp.id) { cancelEdit(); return; }
  editingId.value = vp.id;
  editError.value = "";
  editForm.value = {
    name: vp.name || "",
    mode: vp.mode || "",
    provider: vp.provider || "",
    language: vp.language || "",
    checkpoint_relpath: (vp as Record<string, unknown>).checkpoint_relpath as string || "",
    index_relpath: (vp as Record<string, unknown>).index_relpath as string || "",
    config_relpath: (vp as Record<string, unknown>).config_relpath as string || "",
  };
  editSamplePathsText.value = parseSamplePaths(vp).join("\n");
}

function cancelEdit() {
  editingId.value = null;
  editError.value = "";
}

async function saveEdit(id: number) {
  saving.value = true;
  editError.value = "";
  try {
    const samplePaths = samplePathsToList(editSamplePathsText.value);
    await api.updateVoiceProfile(id, {
      name: editForm.value.name.trim(),
      mode: editForm.value.mode || undefined,
      provider: editForm.value.provider || undefined,
      language: editForm.value.language || undefined,
      sample_relpaths: samplePaths,
      checkpoint_relpath: editForm.value.checkpoint_relpath || undefined,
      index_relpath: editForm.value.index_relpath || undefined,
      config_relpath: editForm.value.config_relpath || undefined,
    });
    cancelEdit();
    await load();
  } catch (e: unknown) {
    editError.value = e instanceof Error ? e.message : String(e);
  } finally {
    saving.value = false;
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

async function deleteProfile(vp: VoiceProfile) {
  if (!window.confirm(`确认删除音色「${vp.name}」？\n\n删除后已绑定此音色的任务/片段将失去关联，该操作不可撤销。`)) return;
  deleting.value[vp.id] = true;
  try {
    await api.deleteVoiceProfile(vp.id);
    if (editingId.value === vp.id) cancelEdit();
    await load();
  } catch (e: unknown) {
    alert(e instanceof Error ? e.message : String(e));
  } finally {
    deleting.value[vp.id] = false;
  }
}

onMounted(load);
</script>
