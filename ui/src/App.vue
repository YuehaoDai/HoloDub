<template>
  <div class="flex h-screen overflow-hidden bg-[#0f1117] text-[#f2f5f7]">
    <!-- 左侧 Sidebar -->
    <aside class="w-64 flex-shrink-0 flex flex-col bg-[#0f1117] border-r border-[#1e2535]">
      <div class="p-4 border-b border-[#1e2535]">
        <h1 class="text-base font-semibold text-white tracking-wide">HoloDub Console</h1>
        <div class="mt-2">
          <input
            v-model="apiKey"
            type="password"
            placeholder="API Key (optional)"
            class="w-full text-xs px-2 py-1.5 rounded bg-[#1e2535] border border-[#273246] text-[#9db0c9] placeholder-[#37465f] focus:outline-none focus:border-[#4a6080]"
            @change="onApiKeyChange"
          />
        </div>
      </div>

      <JobSidebar :selected-job-id="selectedJobId" @select="selectJob" />

      <div class="p-3 border-t border-[#1e2535]">
        <button
          class="w-full text-xs px-3 py-2 rounded bg-blue-600 hover:bg-blue-500 text-white font-medium transition-colors"
          @click="showCreateJob = true"
        >
          + 新建任务
        </button>
      </div>
    </aside>

    <!-- 右侧主内容区 -->
    <main class="flex-1 overflow-auto flex flex-col">
      <!-- TTS 模型预热提示 -->
      <div
        v-if="ttsWarmupStatus === 'loading'"
        class="flex-shrink-0 flex items-center justify-center gap-2 py-2 bg-amber-900/90 text-amber-200 text-sm"
      >
        <span class="animate-pulse">⏳</span>
        TTS 模型预热中，首次 TTS 请求将更快完成…
      </div>
      <div class="flex-1 overflow-auto">
        <router-view />
      </div>
    </main>

    <!-- 创建任务弹窗 -->
    <Transition name="modal">
      <div v-if="showCreateJob" class="fixed inset-0 bg-black/60 flex items-center justify-center z-50" @click.self="showCreateJob = false">
        <div class="bg-[#1e2535] border border-[#273246] rounded-xl p-6 w-full max-w-md shadow-2xl">
          <h2 class="text-sm font-semibold mb-4 text-white">新建转录任务</h2>
          <form @submit.prevent="createJob" class="space-y-3">
            <div>
              <label class="block text-xs text-[#9db0c9] mb-1">任务名称</label>
              <input v-model="newJob.name" type="text" placeholder="my-video" class="hd-input" />
            </div>
            <div>
              <label class="block text-xs text-[#9db0c9] mb-1">输入文件路径 *</label>
              <div class="flex gap-2">
                <input v-model="newJob.input_relpath" type="text" placeholder="input.mp4" required class="hd-input flex-1" />
                <button
                  type="button"
                  class="text-xs px-2.5 py-1.5 rounded bg-[#273246] hover:bg-[#37465f] text-[#9db0c9] hover:text-white transition-colors shrink-0"
                  @click="showFilePicker = true"
                >浏览</button>
              </div>
            </div>
            <div class="grid grid-cols-2 gap-3">
              <div>
                <label class="block text-xs text-[#9db0c9] mb-1">源语言</label>
                <input v-model="newJob.source_language" type="text" placeholder="en" class="hd-input" />
              </div>
              <div>
                <label class="block text-xs text-[#9db0c9] mb-1">目标语言 *</label>
                <input v-model="newJob.target_language" type="text" placeholder="zh-CN" required class="hd-input" />
              </div>
            </div>
            <div class="flex items-center gap-2">
              <input v-model="newJob.auto_start" type="checkbox" id="autoStart" class="rounded" />
              <label for="autoStart" class="text-xs text-[#9db0c9]">自动开始</label>
            </div>
            <div v-if="createError" class="text-xs text-red-400 bg-red-900/20 rounded px-2 py-1">{{ createError }}</div>
            <div class="flex gap-2 pt-1">
              <button type="submit" class="flex-1 text-xs px-3 py-2 rounded bg-blue-600 hover:bg-blue-500 text-white font-medium transition-colors">
                创建
              </button>
              <button type="button" class="flex-1 text-xs px-3 py-2 rounded bg-[#273246] hover:bg-[#37465f] text-[#9db0c9] transition-colors" @click="showCreateJob = false">
                取消
              </button>
            </div>
          </form>
        </div>
      </div>
    </Transition>

    <!-- 文件浏览弹窗 -->
    <FilePicker
      v-model="showFilePicker"
      filter="video"
      @select="onFilePickerSelect"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from "vue";
import { useRouter } from "vue-router";
import { api, getApiKey, setApiKey } from "./api";
import JobSidebar from "./components/JobSidebar.vue";
import FilePicker from "./components/FilePicker.vue";

const router = useRouter();
const apiKey = ref(getApiKey());
const selectedJobId = ref<number | null>(null);
const showCreateJob = ref(false);
const showFilePicker = ref(false);
const createError = ref("");
const ttsWarmupStatus = ref<string>("idle");
let mlHealthTimer: ReturnType<typeof setInterval> | null = null;

async function pollMLHealth() {
  try {
    const h = await api.mlHealth();
    ttsWarmupStatus.value = h.tts_warmup_status ?? "idle";
    if (h.tts_warmup_status === "ready" || h.tts_warmup_status === "error") {
      if (mlHealthTimer) { clearInterval(mlHealthTimer); mlHealthTimer = null; }
    }
  } catch {
    ttsWarmupStatus.value = "idle";
  }
}

const newJob = ref({
  name: "",
  input_relpath: "",
  source_language: "en",
  target_language: "zh-CN",
  auto_start: true,
});

function onApiKeyChange() {
  setApiKey(apiKey.value);
}

function onFilePickerSelect(path: string) {
  newJob.value.input_relpath = path;
}

function selectJob(id: number) {
  selectedJobId.value = id;
  router.push(`/jobs/${id}`);
}

async function createJob() {
  createError.value = "";
  try {
    const job = await api.createJob({
      name: newJob.value.name || undefined,
      input_relpath: newJob.value.input_relpath,
      source_language: newJob.value.source_language || undefined,
      target_language: newJob.value.target_language,
      auto_start: newJob.value.auto_start,
    });
    showCreateJob.value = false;
    newJob.value = { name: "", input_relpath: "", source_language: "en", target_language: "zh-CN", auto_start: true };
    selectedJobId.value = job.id;
    router.push(`/jobs/${job.id}`);
  } catch (e: unknown) {
    createError.value = e instanceof Error ? e.message : String(e);
  }
}

onMounted(() => {
  if (apiKey.value) setApiKey(apiKey.value);
  // Only start health polling if the model isn't already in a terminal state.
  // Use 8s interval — fast enough to notice loading state, slow enough not to
  // contribute meaningfully to rate limit consumption.
  pollMLHealth();
  mlHealthTimer = setInterval(pollMLHealth, 8000);
});

onUnmounted(() => {
  if (mlHealthTimer) clearInterval(mlHealthTimer);
});
</script>

<style>
.hd-input {
  @apply w-full text-xs px-2 py-1.5 rounded bg-[#111722] border border-[#273246] text-[#f2f5f7] placeholder-[#37465f] focus:outline-none focus:border-[#4a6080];
}

.modal-enter-active,
.modal-leave-active {
  transition: opacity 0.15s ease;
}
.modal-enter-from,
.modal-leave-to {
  opacity: 0;
}
</style>
