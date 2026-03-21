<template>
  <nav class="flex-1 overflow-y-auto py-2">
    <div v-if="loading" class="px-4 py-2 text-xs text-[#9db0c9]">加载中...</div>
    <div v-else-if="error" class="px-4 py-2 text-xs text-red-400">{{ error }}</div>
    <template v-else>
      <button
        v-for="job in sortedJobs"
        :key="job.id"
        class="w-full text-left px-4 py-2.5 hover:bg-[#1e2535] transition-colors border-b border-[#1a2030] group"
        :class="{ 'bg-[#1e2535] border-l-2 border-l-blue-500': selectedJobId === job.id }"
        @click="$emit('select', job.id)"
      >
        <div class="flex items-center justify-between gap-1">
          <span class="text-xs font-medium text-[#f2f5f7] truncate">{{ job.name || `Job #${job.id}` }}</span>
          <span class="text-[10px] shrink-0 px-1.5 py-0.5 rounded-full" :class="statusClass(job.status)">
            {{ job.status }}
          </span>
        </div>
        <div class="text-[10px] text-[#9db0c9] mt-0.5 truncate">#{{ job.id }} · {{ job.current_stage }}</div>
      </button>
    </template>
    <div class="px-3 pt-2">
      <button
        class="w-full text-xs text-[#9db0c9] hover:text-white py-1 transition-colors"
        @click="load(false)"
      >
        ↻ 刷新列表
      </button>
    </div>
  </nav>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted } from "vue";
import { api, type Job } from "../api";

const props = defineProps<{ selectedJobId: number | null }>();
defineEmits<{ select: [id: number] }>();

const jobs = ref<Job[]>([]);
const loading = ref(false);
const error = ref("");
let timer: ReturnType<typeof setInterval> | null = null;

const sortedJobs = computed(() =>
  [...jobs.value].sort((a, b) => b.id - a.id)
);

function statusClass(status: string) {
  switch (status) {
    case "running": return "bg-blue-900/60 text-blue-300";
    case "completed": return "bg-green-900/60 text-green-300";
    case "failed": return "bg-red-900/60 text-red-300";
    case "cancelled": return "bg-slate-700 text-slate-400";
    default: return "bg-slate-800 text-slate-400";
  }
}

async function load(silent = false) {
  if (!silent) loading.value = true;
  error.value = "";
  try {
    const data = await api.listJobs();
    jobs.value = data.jobs || [];
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    if (!silent) loading.value = false;
  }
}

onMounted(() => {
  load();
  timer = setInterval(() => load(true), 30000);
});

onUnmounted(() => {
  if (timer) clearInterval(timer);
});
</script>
