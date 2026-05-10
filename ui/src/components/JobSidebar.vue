<template>
  <nav class="flex-1 overflow-y-auto py-2 flex flex-col">
    <!-- Tab switcher (OPT-401: Episodes vs Jobs) -->
    <div class="flex gap-1 px-3 pb-2 border-b border-[#1e2535] flex-shrink-0">
      <button
        class="flex-1 text-[10px] py-1.5 rounded transition-colors"
        :class="activeTab === 'jobs'
          ? 'bg-[#1e2535] text-white'
          : 'text-[#9db0c9] hover:text-white'"
        @click="setActiveTab('jobs')"
      >Jobs</button>
      <button
        class="flex-1 text-[10px] py-1.5 rounded transition-colors"
        :class="activeTab === 'episodes'
          ? 'bg-[#1e2535] text-white'
          : 'text-[#9db0c9] hover:text-white'"
        @click="setActiveTab('episodes')"
      >Episodes</button>
    </div>

    <div class="flex-1 overflow-y-auto">
      <div v-if="loading" class="px-4 py-2 text-xs text-[#9db0c9]">加载中...</div>
      <div v-else-if="error" class="px-4 py-2 text-xs text-red-400">{{ error }}</div>

      <template v-else-if="activeTab === 'jobs'">
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

      <template v-else>
        <button
          v-for="ep in sortedEpisodes"
          :key="ep.id"
          class="w-full text-left px-4 py-2.5 hover:bg-[#1e2535] transition-colors border-b border-[#1a2030] group"
          @click="selectEpisode(ep.id)"
        >
          <div class="flex items-center justify-between gap-1">
            <span class="text-xs font-medium text-[#f2f5f7] truncate">{{ ep.name || `Episode #${ep.id}` }}</span>
            <span class="text-[10px] shrink-0 px-1.5 py-0.5 rounded-full" :class="statusClass(ep.status)">
              {{ ep.status }}
            </span>
          </div>
          <div class="text-[10px] text-[#9db0c9] mt-0.5 truncate">
            #{{ ep.id }} · {{ ep.total_chapters }} chapter{{ ep.total_chapters > 1 ? "s" : "" }}
          </div>
        </button>
      </template>
    </div>

    <div class="px-3 pt-2 flex-shrink-0">
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
import { ref, computed, onMounted, onUnmounted, watch } from "vue";
import { useRouter, useRoute } from "vue-router";
import { api, type Job, type Episode } from "../api";

defineProps<{ selectedJobId: number | null }>();
defineEmits<{ select: [id: number] }>();

type Tab = "jobs" | "episodes";

const route = useRoute();
const router = useRouter();
const jobs = ref<Job[]>([]);
const episodes = ref<Episode[]>([]);
const loading = ref(false);
const error = ref("");
const activeTab = ref<Tab>(deriveInitialTab());
let timer: ReturnType<typeof setInterval> | null = null;

const sortedJobs = computed(() =>
  [...jobs.value].sort((a, b) => b.id - a.id)
);

const sortedEpisodes = computed(() =>
  [...episodes.value].sort((a, b) => b.id - a.id)
);

function deriveInitialTab(): Tab {
  // Honour the URL on first paint so a deep link to /episodes/:id keeps the
  // sidebar tab in sync without a flash of the wrong list.
  return route.path.startsWith("/episodes") ? "episodes" : "jobs";
}

function setActiveTab(tab: Tab) {
  if (activeTab.value === tab) return;
  activeTab.value = tab;
  load(false);
}

function selectEpisode(id: number) {
  router.push(`/episodes/${id}`);
}

function statusClass(status: string) {
  switch (status) {
    case "running":
    case "chaptering":
    case "dispatched":
    case "merging":
    case "judging":
    case "reworking":
      return "bg-blue-900/60 text-blue-300";
    case "completed": return "bg-green-900/60 text-green-300";
    case "failed":
    case "timed_out":
      return "bg-red-900/60 text-red-300";
    case "cancelled": return "bg-slate-700 text-slate-400";
    default: return "bg-slate-800 text-slate-400";
  }
}

async function load(silent = false) {
  if (!silent) loading.value = true;
  error.value = "";
  try {
    if (activeTab.value === "jobs") {
      const data = await api.listJobs();
      jobs.value = data.jobs || [];
    } else {
      const data = await api.listEpisodes();
      episodes.value = data.episodes || [];
    }
  } catch (e: unknown) {
    error.value = e instanceof Error ? e.message : String(e);
  } finally {
    if (!silent) loading.value = false;
  }
}

watch(
  () => route.path,
  (path) => {
    if (path.startsWith("/episodes") && activeTab.value !== "episodes") {
      activeTab.value = "episodes";
      load(false);
    }
  }
);

onMounted(() => {
  load();
  timer = setInterval(() => load(true), 30000);
});

onUnmounted(() => {
  if (timer) clearInterval(timer);
});
</script>
