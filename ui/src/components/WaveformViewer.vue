<template>
  <div class="waveform-viewer">
    <label v-if="label" class="hd-label mb-1 block">{{ label }}</label>
    <div
      ref="containerRef"
      class="min-h-[80px] rounded-[8px] border border-[color:var(--hd-border-soft)] bg-black/25"
    ></div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, onBeforeUnmount, watch } from "vue";
import WaveSurfer from "wavesurfer.js";

const props = defineProps<{
  audioUrl: string;
  label?: string;
}>();

const containerRef = ref<HTMLDivElement | null>(null);
let wavesurfer: WaveSurfer | null = null;

function initWaveSurfer() {
  if (!containerRef.value || !props.audioUrl) return;
  wavesurfer?.destroy();
  wavesurfer = WaveSurfer.create({
    container: containerRef.value,
    url: props.audioUrl,
    waveColor: "#455565",
    progressColor: "#20d5ee",
    height: 80,
    barWidth: 1,
    barGap: 1,
  });
}

onMounted(() => {
  initWaveSurfer();
});

onBeforeUnmount(() => {
  wavesurfer?.destroy();
  wavesurfer = null;
});

watch(
  () => props.audioUrl,
  () => {
    initWaveSurfer();
  },
);
</script>

<style scoped>
.waveform-viewer :deep(wave) {
  border-radius: 4px;
}
</style>
