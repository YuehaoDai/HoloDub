<template>
  <div class="hd-app">
    <aside class="hd-rail">
      <div class="border-b border-[color:var(--hd-border-soft)] p-4">
        <div class="flex items-center gap-3">
          <div
            class="flex h-9 w-9 items-center justify-center rounded-[8px] border border-cyan-400/30 bg-cyan-400/10 text-cyan-200"
          >
            <Clapperboard :size="19" />
          </div>
          <div class="min-w-0">
            <h1 class="text-sm font-semibold text-white">HoloDub Console</h1>
            <p class="mt-0.5 text-[10px] text-[color:var(--hd-text-dim)]">
              {{ t("配音可观测工作台") }}
            </p>
          </div>
        </div>
        <div class="mt-4">
          <div class="hd-label mb-1.5 flex items-center gap-1.5">
            <Globe2 :size="12" />
            {{ t("语言") }}
          </div>
          <div
            class="grid grid-cols-2 gap-1 rounded-[8px] border border-[color:var(--hd-border-soft)] bg-black/20 p-1"
          >
            <button
              v-for="opt in localeOptions"
              :key="opt.value"
              class="rounded-[6px] px-2 py-1.5 text-xs font-semibold transition"
              :class="
                locale === opt.value
                  ? 'bg-[color:var(--hd-surface-3)] text-white shadow-sm'
                  : 'text-[color:var(--hd-text-muted)] hover:text-white'
              "
              :aria-pressed="locale === opt.value"
              @click="setLocale(opt.value)"
            >
              {{ opt.label }}
            </button>
          </div>
        </div>
        <label class="mt-4 block">
          <span class="hd-label mb-1.5 flex items-center gap-1.5">
            <KeyRound :size="12" />
            API Key
          </span>
          <input
            v-model="apiKey"
            type="password"
            placeholder="Optional for local smoke mode"
            class="hd-input"
            @change="onApiKeyChange"
          />
        </label>
      </div>

      <JobSidebar :selected-job-id="selectedJobId" @select="selectJob" />

      <div class="space-y-2 border-t border-[color:var(--hd-border-soft)] p-3">
        <button
          class="hd-btn w-full justify-start"
          :class="
            route.path.startsWith('/lora-training')
              ? 'hd-btn-secondary text-cyan-100'
              : 'hd-btn-ghost'
          "
          @click="openLoraTraining"
        >
          <FlaskConical :size="15" />
          {{ t("LoRA Training") }}
        </button>
        <button
          class="hd-btn w-full justify-start"
          :class="
            route.path.startsWith('/lora-adapters')
              ? 'hd-btn-secondary text-cyan-100'
              : 'hd-btn-ghost'
          "
          @click="openLoraAdapters"
        >
          <BrainCircuit :size="15" />
          {{ t("LoRA Adapters") }}
        </button>
        <button class="hd-btn hd-btn-primary w-full" @click="showCreateJob = true">
          <Plus :size="15" />
          {{ t("新建任务") }}
        </button>
      </div>
    </aside>

    <main class="hd-main flex flex-col">
      <div
        v-if="ttsWarmupStatus === 'loading'"
        class="flex-shrink-0 border-b border-amber-400/20 bg-amber-500/10 px-4 py-2 text-xs text-amber-100"
      >
        <div class="mx-auto flex max-w-[1680px] items-center justify-center gap-2">
          <LoaderCircle :size="14" class="animate-spin text-amber-300" />
          <span>{{ t("TTS 模型预热中，首次 TTS 请求将更快完成") }}</span>
        </div>
      </div>
      <div class="flex-1 overflow-auto">
        <router-view />
      </div>
    </main>

    <!-- 创建任务弹窗 -->
    <Transition name="modal">
      <div v-if="showCreateJob" class="hd-modal-backdrop" @click.self="showCreateJob = false">
        <div class="hd-modal">
          <div class="mb-5 flex items-start justify-between gap-4">
            <div>
              <h2 class="text-base font-semibold text-white">{{ t("新建转录任务") }}</h2>
              <p class="mt-1 text-xs text-[color:var(--hd-text-muted)]">
                {{ t("提交视频后可在任务工作台跟踪 ASR、翻译、TTS 和合并状态。") }}
              </p>
            </div>
            <button
              type="button"
              class="hd-icon-btn"
              :aria-label="t('关闭')"
              @click="showCreateJob = false"
            >
              <X :size="15" />
            </button>
          </div>
          <form @submit.prevent="createJob" class="space-y-3">
            <div>
              <label class="mb-1.5 block text-xs text-[color:var(--hd-text-muted)]">{{
                t("任务名称")
              }}</label>
              <input v-model="newJob.name" type="text" placeholder="my-video" class="hd-input" />
            </div>
            <div>
              <label class="mb-1.5 block text-xs text-[color:var(--hd-text-muted)]">{{
                t("输入文件路径 *")
              }}</label>
              <div class="flex gap-2">
                <input
                  v-model="newJob.input_relpath"
                  type="text"
                  placeholder="input.mp4"
                  required
                  class="hd-input flex-1"
                />
                <button
                  type="button"
                  class="hd-btn hd-btn-secondary shrink-0"
                  @click="showFilePicker = true"
                >
                  <FolderOpen :size="14" />
                  {{ t("浏览") }}
                </button>
              </div>
            </div>
            <div class="grid grid-cols-2 gap-3">
              <div>
                <label class="mb-1.5 block text-xs text-[color:var(--hd-text-muted)]">{{
                  t("源语言")
                }}</label>
                <input
                  v-model="newJob.source_language"
                  type="text"
                  placeholder="en"
                  class="hd-input"
                />
              </div>
              <div>
                <label class="mb-1.5 block text-xs text-[color:var(--hd-text-muted)]">{{
                  t("目标语言 *")
                }}</label>
                <input
                  v-model="newJob.target_language"
                  type="text"
                  placeholder="zh-CN"
                  required
                  class="hd-input"
                />
              </div>
            </div>
            <div class="flex items-center gap-2">
              <input
                v-model="newJob.auto_start"
                type="checkbox"
                id="autoStart"
                class="rounded border-[color:var(--hd-border)] bg-[color:var(--hd-surface)]"
              />
              <label for="autoStart" class="text-xs text-[color:var(--hd-text-muted)]">{{
                t("创建后自动开始流水线")
              }}</label>
            </div>
            <div
              v-if="createError"
              class="rounded-[7px] border border-rose-400/20 bg-rose-500/10 px-3 py-2 text-xs text-rose-200"
            >
              {{ createError }}
            </div>
            <div class="flex gap-2 pt-2">
              <button type="submit" class="hd-btn hd-btn-primary flex-1">{{ t("创建") }}</button>
              <button
                type="button"
                class="hd-btn hd-btn-secondary flex-1"
                @click="showCreateJob = false"
              >
                {{ t("取消") }}
              </button>
            </div>
          </form>
        </div>
      </div>
    </Transition>

    <!-- 文件浏览弹窗 -->
    <FilePicker v-model="showFilePicker" filter="video" @select="onFilePickerSelect" />

    <!-- 全局 toast 容器 -->
    <ToastContainer />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onUnmounted } from "vue";
import { useRoute, useRouter } from "vue-router";
import {
  BrainCircuit,
  Clapperboard,
  FlaskConical,
  FolderOpen,
  Globe2,
  KeyRound,
  LoaderCircle,
  Plus,
  X,
} from "@lucide/vue";
import { api, getApiKey, setApiKey } from "./api";
import { useI18n } from "./i18n";
import { toast } from "./lib/toast";
import JobSidebar from "./components/JobSidebar.vue";
import FilePicker from "./components/FilePicker.vue";
import ToastContainer from "./components/ToastContainer.vue";

const router = useRouter();
const route = useRoute();
const { locale, localeOptions, setLocale, t } = useI18n();
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
      if (mlHealthTimer) {
        clearInterval(mlHealthTimer);
        mlHealthTimer = null;
      }
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

function openLoraAdapters() {
  selectedJobId.value = null;
  router.push("/lora-adapters");
}

function openLoraTraining() {
  selectedJobId.value = null;
  router.push("/lora-training");
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
    newJob.value = {
      name: "",
      input_relpath: "",
      source_language: "en",
      target_language: "zh-CN",
      auto_start: true,
    };
    selectedJobId.value = job.id;
    router.push(`/jobs/${job.id}`);
    toast.success(t("任务 #{id} 已创建", { id: job.id }));
  } catch (e: unknown) {
    createError.value = e instanceof Error ? e.message : String(e);
    toast.fromError(e, t("创建任务失败"));
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
.modal-enter-active,
.modal-leave-active {
  transition: opacity 0.16s ease;
}
.modal-enter-from,
.modal-leave-to {
  opacity: 0;
}
</style>
