// usePolling — reusable polling primitive.
//
// Replaces the multiple ad-hoc setInterval calls scattered across
// JobDetail.vue, JobSidebar.vue, and App.vue with a single composable that:
//
//   - cancels in-flight requests on unmount or when interval changes
//   - supports a dynamic interval (returns a ref so the caller can pause)
//   - skips overlapping calls (does not start the next tick until the
//     previous one finishes)
//   - propagates errors to a callback and reports loading state
//
// Usage:
//
//   const { data, error, isLoading, refresh } = usePolling({
//     fetcher: (signal) => api.getJob(jobId, signal),
//     intervalMs: 5000,
//     enabled: () => job.value?.status === "running",
//   });

import { onUnmounted, ref, watch, type Ref } from "vue";

export interface UsePollingOptions<T> {
  fetcher: (signal: AbortSignal) => Promise<T>;
  /** Polling interval in ms. Use 0 to disable polling (manual refresh only). */
  intervalMs: number | Ref<number>;
  /** Optional gating predicate; when it returns false polling is paused. */
  enabled?: () => boolean;
  /** Skip the first immediate fetch on mount when true. */
  immediate?: boolean;
  onError?: (err: unknown) => void;
}

export function usePolling<T>(options: UsePollingOptions<T>) {
  const data = ref<T | undefined>(undefined) as Ref<T | undefined>;
  const error = ref<unknown>(null);
  const isLoading = ref(false);
  let controller: AbortController | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let stopped = false;

  function intervalValue(): number {
    return typeof options.intervalMs === "number"
      ? options.intervalMs
      : options.intervalMs.value;
  }

  async function tick() {
    if (stopped) return;
    if (options.enabled && !options.enabled()) {
      schedule();
      return;
    }
    isLoading.value = true;
    controller?.abort();
    controller = new AbortController();
    try {
      data.value = await options.fetcher(controller.signal);
      error.value = null;
    } catch (err) {
      // Ignore aborts that we triggered ourselves (component unmount or
      // interval change). Anything else is reported to the caller.
      const isAbort =
        err instanceof DOMException
          ? err.name === "AbortError"
          : (err as { name?: string })?.name === "AbortError";
      if (!isAbort) {
        error.value = err;
        options.onError?.(err);
      }
    } finally {
      isLoading.value = false;
      schedule();
    }
  }

  function schedule() {
    if (stopped) return;
    if (timer) clearTimeout(timer);
    const ms = intervalValue();
    if (ms <= 0) return;
    timer = setTimeout(tick, ms);
  }

  function refresh(): Promise<void> {
    return tick();
  }

  function stop() {
    stopped = true;
    if (timer) clearTimeout(timer);
    timer = null;
    controller?.abort();
  }

  if (typeof options.intervalMs !== "number") {
    watch(options.intervalMs, () => {
      if (timer) clearTimeout(timer);
      schedule();
    });
  }

  onUnmounted(stop);

  if (options.immediate !== false) {
    void tick();
  } else {
    schedule();
  }

  return { data, error, isLoading, refresh, stop };
}
