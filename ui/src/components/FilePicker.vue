<template>
  <Teleport to="body">
    <Transition name="modal">
      <div
        v-if="modelValue"
        class="fixed inset-0 bg-black/70 flex items-center justify-center z-[60]"
        @click.self="$emit('update:modelValue', false)"
      >
        <div class="bg-[#1e2535] border border-[#273246] rounded-xl shadow-2xl flex flex-col w-[600px] max-w-[95vw] max-h-[80vh]">
          <!-- Header -->
          <div class="flex items-center justify-between px-4 py-3 border-b border-[#273246] shrink-0">
            <span class="text-sm font-semibold text-white">
              {{ multiple ? '选择文件（可多选）' : '选择文件' }}
            </span>
            <button class="text-[#9db0c9] hover:text-white transition-colors text-lg leading-none" @click="$emit('update:modelValue', false)">×</button>
          </div>

          <!-- Breadcrumb -->
          <div class="flex items-center gap-1 px-4 py-2 bg-[#111722] border-b border-[#273246] shrink-0 overflow-x-auto">
            <button
              class="text-[10px] text-[#9db0c9] hover:text-white transition-colors shrink-0"
              @click="navigateTo('')"
            >
              DATA_ROOT
            </button>
            <template v-for="(seg, i) in pathSegments" :key="i">
              <span class="text-[#37465f] shrink-0">/</span>
              <button
                class="text-[10px] transition-colors shrink-0"
                :class="i === pathSegments.length - 1 ? 'text-white cursor-default' : 'text-[#9db0c9] hover:text-white'"
                @click="i < pathSegments.length - 1 && navigateTo(breadcrumbPath(i))"
              >
                {{ seg }}
              </button>
            </template>
          </div>

          <!-- File List -->
          <div class="flex-1 overflow-y-auto">
            <div v-if="loading" class="flex items-center justify-center py-8 text-xs text-[#9db0c9]">加载中…</div>
            <div v-else-if="loadError" class="px-4 py-4 text-xs text-red-400">{{ loadError }}</div>
            <div v-else-if="!entries.length" class="px-4 py-8 text-xs text-center text-[#37465f]">此目录为空</div>
            <div v-else>
              <!-- Parent dir entry -->
              <button
                v-if="currentDir !== ''"
                class="w-full flex items-center gap-3 px-4 py-2.5 hover:bg-[#273246] transition-colors text-left"
                @click="navigateUp"
              >
                <span class="text-base shrink-0">📁</span>
                <span class="text-xs text-[#9db0c9]">..</span>
              </button>
              <!-- Entries -->
              <div
                v-for="entry in sortedEntries"
                :key="entry.relpath"
                class="flex items-center gap-3 px-4 py-2.5 hover:bg-[#273246] transition-colors cursor-pointer"
                :class="selectedPaths.has(entry.relpath) && !entry.is_dir ? 'bg-blue-900/30' : ''"
                @click="onEntryClick(entry)"
              >
                <!-- Checkbox (multi only, files only) -->
                <input
                  v-if="multiple && !entry.is_dir"
                  type="checkbox"
                  class="shrink-0 accent-blue-500"
                  :checked="selectedPaths.has(entry.relpath)"
                  @click.stop
                  @change="toggleSelect(entry.relpath)"
                />
                <!-- Icon -->
                <span class="text-base shrink-0">{{ entryIcon(entry) }}</span>
                <!-- Name -->
                <span class="flex-1 text-xs truncate" :class="entry.is_dir ? 'text-[#9db0c9]' : 'text-[#f2f5f7]'">
                  {{ entry.name }}
                </span>
                <!-- Size -->
                <span v-if="!entry.is_dir && entry.size_bytes" class="text-[10px] text-[#37465f] shrink-0">
                  {{ formatBytes(entry.size_bytes) }}
                </span>
                <!-- Dir arrow -->
                <span v-if="entry.is_dir" class="text-[#37465f] shrink-0 text-xs">›</span>
              </div>
            </div>
          </div>

          <!-- Footer (multi-select confirm / single-select info) -->
          <div class="flex items-center justify-between px-4 py-3 border-t border-[#273246] shrink-0">
            <span class="text-[10px] text-[#37465f]">
              {{ multiple ? `已选 ${selectedPaths.size} 个文件` : '点击文件直接选中' }}
            </span>
            <div class="flex gap-2">
              <button
                v-if="multiple"
                class="text-xs px-3 py-1.5 rounded bg-blue-600 hover:bg-blue-500 text-white transition-colors disabled:opacity-50"
                :disabled="selectedPaths.size === 0"
                @click="confirmMultiple"
              >
                添加 {{ selectedPaths.size > 0 ? `(${selectedPaths.size})` : '' }}
              </button>
              <button
                class="text-xs px-3 py-1.5 rounded bg-[#273246] hover:bg-[#37465f] text-[#9db0c9] transition-colors"
                @click="$emit('update:modelValue', false)"
              >
                取消
              </button>
            </div>
          </div>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, watch } from "vue";
import { api, type FileEntry } from "../api";

const props = withDefaults(defineProps<{
  modelValue: boolean;
  filter?: "video" | "audio" | "all";
  multiple?: boolean;
}>(), {
  filter: "all",
  multiple: false,
});

const emit = defineEmits<{
  "update:modelValue": [value: boolean];
  select: [path: string];
  selectMultiple: [paths: string[]];
}>();

const currentDir = ref("");
const entries = ref<FileEntry[]>([]);
const loading = ref(false);
const loadError = ref("");
const selectedPaths = ref(new Set<string>());

const pathSegments = computed(() => currentDir.value ? currentDir.value.split("/") : []);

const sortedEntries = computed(() => {
  const dirs = entries.value.filter(e => e.is_dir);
  const files = entries.value.filter(e => !e.is_dir);
  return [...dirs, ...files];
});

async function loadDir(dir: string) {
  loading.value = true;
  loadError.value = "";
  try {
    const res = await api.listFiles(dir || undefined, props.filter);
    entries.value = res.entries;
    currentDir.value = res.dir;
  } catch (e: unknown) {
    loadError.value = e instanceof Error ? e.message : String(e);
  } finally {
    loading.value = false;
  }
}

function navigateTo(dir: string) {
  selectedPaths.value = new Set();
  loadDir(dir);
}

function navigateUp() {
  const parts = currentDir.value.split("/");
  parts.pop();
  navigateTo(parts.join("/"));
}

function breadcrumbPath(segIndex: number) {
  return pathSegments.value.slice(0, segIndex + 1).join("/");
}

function onEntryClick(entry: FileEntry) {
  if (entry.is_dir) {
    navigateTo(entry.relpath);
  } else if (props.multiple) {
    toggleSelect(entry.relpath);
  } else {
    emit("select", entry.relpath);
    emit("update:modelValue", false);
  }
}

function toggleSelect(path: string) {
  const s = new Set(selectedPaths.value);
  if (s.has(path)) s.delete(path);
  else s.add(path);
  selectedPaths.value = s;
}

function confirmMultiple() {
  emit("selectMultiple", [...selectedPaths.value]);
  emit("update:modelValue", false);
}

function entryIcon(entry: FileEntry): string {
  if (entry.is_dir) return "📁";
  const ext = entry.name.split(".").pop()?.toLowerCase() ?? "";
  if (["mp4", "mkv", "avi", "mov", "webm", "ts", "flv"].includes(ext)) return "🎬";
  if (["wav", "mp3", "flac", "m4a", "ogg", "aac"].includes(ext)) return "🎵";
  return "📄";
}

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

// Load root when opened
watch(() => props.modelValue, (val) => {
  if (val) {
    currentDir.value = "";
    entries.value = [];
    selectedPaths.value = new Set();
    loadDir("");
  }
});
</script>
