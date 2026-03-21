<template>
  <div class="flex flex-wrap items-center gap-2 mb-3 p-2 bg-[#171b26] rounded-lg border border-[#1e2535]">
    <!-- 筛选 -->
    <div class="flex items-center gap-1 text-xs">
      <span class="text-[#9db0c9] mr-1">筛选：</span>
      <button
        v-for="opt in filterOptions"
        :key="opt.value"
        class="px-2 py-1 rounded transition-colors"
        :class="filter === opt.value ? 'bg-blue-600 text-white' : 'bg-[#1e2535] text-[#9db0c9] hover:text-white'"
        @click="$emit('update:filter', opt.value)"
      >
        {{ opt.label }}
        <span v-if="opt.value === 'high-drift' && highDriftCount" class="ml-1 text-[10px] bg-red-800/60 text-red-300 px-1 rounded-full">
          {{ highDriftCount }}
        </span>
      </button>
    </div>

    <div class="h-4 w-px bg-[#273246] mx-1"></div>

    <!-- 快捷键 -->
    <span class="text-[10px] text-[#37465f]">J/K 导航 · Space 播放 · E 编辑</span>

    <div class="h-4 w-px bg-[#273246] mx-1"></div>

    <!-- 排序 -->
    <div class="flex items-center gap-1 text-xs">
      <span class="text-[#9db0c9] mr-1">排序：</span>
      <button
        v-for="opt in sortOptions"
        :key="opt.value"
        class="px-2 py-1 rounded transition-colors"
        :class="sort === opt.value ? 'bg-[#273246] text-white' : 'bg-transparent text-[#9db0c9] hover:text-white'"
        @click="toggleSort(opt.value)"
      >
        {{ opt.label }}
        <span v-if="sort === opt.value">{{ sortDir === 'asc' ? '↑' : '↓' }}</span>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
const emit = defineEmits<{
  "update:filter": [v: "all" | "high-drift" | "unsynthesized"];
  "update:sort": [v: "ordinal" | "drift"];
  "update:sortDir": [v: "asc" | "desc"];
}>();

const filterOptions = [
  { value: "all" as const, label: "全部" },
  { value: "high-drift" as const, label: "高漂移" },
  { value: "unsynthesized" as const, label: "未合成" },
];

const sortOptions = [
  { value: "ordinal" as const, label: "序号" },
  { value: "drift" as const, label: "漂移率" },
];

const props = defineProps<{
  filter: "all" | "high-drift" | "unsynthesized";
  sort: "ordinal" | "drift";
  sortDir: "asc" | "desc";
  highDriftCount: number;
}>();

function toggleSort(value: "ordinal" | "drift") {
  if (props.sort === value) {
    emit("update:sortDir", props.sortDir === "asc" ? "desc" : "asc");
  } else {
    emit("update:sort", value);
    emit("update:sortDir", "asc");
  }
}
</script>
