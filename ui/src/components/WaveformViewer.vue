<template>
  <div class="waveform-viewer">
    <label v-if="label" class="block text-[10px] text-[#9db0c9] mb-1">{{ label }}</label>
    <div ref="containerRef" class="min-h-[80px] rounded bg-[#1e2535]"></div>
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
    waveColor: "#4a6080",
    progressColor: "#60a5fa",
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
  }
);
</script>

<style scoped>
.waveform-viewer :deep(wave) {
  border-radius: 4px;
}
</style>
