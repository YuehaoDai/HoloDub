<template>
  <Teleport to="body">
    <div class="pointer-events-none fixed bottom-6 right-6 z-[9999] flex flex-col gap-2 max-w-sm">
      <TransitionGroup name="toast">
        <div
          v-for="t in toasts"
          :key="t.id"
          :class="[
            'pointer-events-auto rounded-md shadow-lg border px-4 py-3 text-sm flex items-start gap-3',
            variantClass(t.variant),
          ]"
        >
          <div class="flex-1 min-w-0">
            <div class="font-medium leading-tight">{{ t.message }}</div>
            <div v-if="t.detail" class="mt-1 text-xs opacity-80 break-all">{{ t.detail }}</div>
          </div>
          <button
            type="button"
            class="text-xs opacity-60 hover:opacity-100 transition-opacity"
            aria-label="dismiss"
            @click="dismiss(t.id)"
          >
            ✕
          </button>
        </div>
      </TransitionGroup>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { useToasts, dismiss, type ToastVariant } from "../lib/toast";

const toasts = useToasts();

function variantClass(v: ToastVariant): string {
  switch (v) {
    case "success":
      return "bg-emerald-900/95 border-emerald-700 text-emerald-100";
    case "warning":
      return "bg-amber-900/95 border-amber-700 text-amber-100";
    case "error":
      return "bg-red-900/95 border-red-700 text-red-100";
    case "info":
    default:
      return "bg-slate-800/95 border-slate-700 text-slate-100";
  }
}
</script>

<style scoped>
.toast-enter-active,
.toast-leave-active {
  transition: all 0.2s ease;
}
.toast-enter-from {
  opacity: 0;
  transform: translateX(20px);
}
.toast-leave-to {
  opacity: 0;
  transform: translateX(20px);
}
</style>
