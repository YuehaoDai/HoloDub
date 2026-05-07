// Lightweight toast / notice store. Replaces the scattered window.alert()
// calls in JobDetail.vue / SegmentTable.vue with a single non-blocking
// notification surface.
//
// Intentionally tiny — no library, no animation framework. A future PR can
// swap this for naive-ui's <NMessageProvider> or a similar component without
// touching call-sites.

import { ref, type Ref } from "vue";

export type ToastVariant = "info" | "success" | "warning" | "error";

export interface Toast {
  id: number;
  variant: ToastVariant;
  message: string;
  detail?: string;
  /** When set, the toast auto-dismisses after this many ms (default 5000). */
  durationMs?: number;
}

const _toasts: Ref<Toast[]> = ref([]);
let _nextId = 1;

export function useToasts() {
  return _toasts;
}

function push(variant: ToastVariant, message: string, detail?: string, durationMs?: number): void {
  const id = _nextId++;
  const toast: Toast = { id, variant, message, detail, durationMs };
  _toasts.value = [..._toasts.value, toast];
  const ttl = durationMs ?? 5000;
  if (ttl > 0) {
    setTimeout(() => dismiss(id), ttl);
  }
}

export function dismiss(id: number): void {
  _toasts.value = _toasts.value.filter((t) => t.id !== id);
}

export function clearToasts(): void {
  _toasts.value = [];
}

export const toast = {
  info: (message: string, detail?: string, durationMs?: number) =>
    push("info", message, detail, durationMs),
  success: (message: string, detail?: string, durationMs?: number) =>
    push("success", message, detail, durationMs),
  warning: (message: string, detail?: string, durationMs?: number) =>
    push("warning", message, detail, durationMs),
  error: (message: string, detail?: string, durationMs?: number) =>
    push("error", message, detail, durationMs ?? 8000),
  /**
   * Convenience: show an error from a thrown value with sensible defaults.
   * Recognises ApiError (already imported by callers).
   */
  fromError(err: unknown, fallback = "操作失败"): void {
    if (err instanceof Error) {
      const code = (err as { code?: string }).code;
      const detail = code ? `[${code}]` : undefined;
      push("error", err.message || fallback, detail, 8000);
      return;
    }
    push("error", typeof err === "string" ? err : fallback);
  },
};
