<template>
  <div class="hd-toolbar mb-3">
    <div class="flex items-center gap-2 text-xs">
      <span class="hd-chip hd-chip-muted">
        <SlidersHorizontal :size="12" />
        {{ t("筛选") }}
      </span>
      <button
        v-for="opt in filterOptions"
        :key="opt.value"
        class="rounded-[7px] px-2.5 py-1.5 font-semibold transition"
        :class="
          filter === opt.value
            ? 'bg-cyan-300/15 text-cyan-100 ring-1 ring-cyan-300/25'
            : 'text-[color:var(--hd-text-muted)] hover:bg-white/[0.05] hover:text-white'
        "
        @click="$emit('update:filter', opt.value)"
      >
        {{ opt.label }}
        <span
          v-if="opt.value === 'high-drift' && highDriftCount"
          class="ml-1 rounded-full bg-rose-400/20 px-1.5 py-0.5 text-[10px] text-rose-200"
        >
          {{ highDriftCount }}
        </span>
      </button>
    </div>

    <div class="h-5 w-px bg-[color:var(--hd-border-soft)]"></div>

    <span class="hidden text-[10px] text-[color:var(--hd-text-dim)] md:inline">{{
      t("J/K 导航 · Space 播放 · E 编辑")
    }}</span>

    <div class="h-5 w-px bg-[color:var(--hd-border-soft)]"></div>

    <div class="ml-auto flex items-center gap-2 text-xs">
      <span class="hd-chip hd-chip-muted">
        <ArrowUpDown :size="12" />
        {{ t("排序") }}
      </span>
      <button
        v-for="opt in sortOptions"
        :key="opt.value"
        class="rounded-[7px] px-2.5 py-1.5 font-semibold transition"
        :class="
          sort === opt.value
            ? 'bg-white/[0.08] text-white'
            : 'text-[color:var(--hd-text-muted)] hover:bg-white/[0.05] hover:text-white'
        "
        @click="toggleSort(opt.value)"
      >
        {{ opt.label }}
        <span v-if="sort === opt.value" class="ml-1 text-cyan-200">{{
          sortDir === "asc" ? "↑" : "↓"
        }}</span>
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ArrowUpDown, SlidersHorizontal } from "@lucide/vue";
import { computed } from "vue";
import { useI18n } from "../i18n";

const { t } = useI18n();

const emit = defineEmits<{
  "update:filter": [v: "all" | "high-drift" | "unsynthesized"];
  "update:sort": [v: "ordinal" | "drift"];
  "update:sortDir": [v: "asc" | "desc"];
}>();

const filterOptions = computed(() =>
  [
    { value: "all" as const, label: "全部" },
    { value: "high-drift" as const, label: "高漂移" },
    { value: "unsynthesized" as const, label: "未合成" },
  ].map((option) => ({ ...option, label: t(option.label) })),
);

const sortOptions = computed(() =>
  [
    { value: "ordinal" as const, label: "序号" },
    { value: "drift" as const, label: "漂移率" },
  ].map((option) => ({ ...option, label: t(option.label) })),
);

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
