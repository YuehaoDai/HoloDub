// Shared HTTP client used by every API call in the SPA.
//
// Goals over the previous one-line `apiFetch` helper:
//   - structured errors (ApiError) so callers can branch on status / code
//   - per-request timeout (AbortSignal.timeout when supported, fallback otherwise)
//   - automatic merge of API key headers
//   - opt-in retry hook (single retry on network errors / 5xx)
//
// Lives under src/lib/ so it has zero Vue dependencies and can be unit-tested
// in isolation (Vitest, when added).

import { getApiKey } from "../api";

export class ApiError extends Error {
  status: number;
  code?: string;
  payload?: unknown;
  retryable: boolean;
  constructor(opts: {
    status: number;
    code?: string;
    message: string;
    payload?: unknown;
    retryable?: boolean;
  }) {
    super(opts.message);
    this.name = "ApiError";
    this.status = opts.status;
    this.code = opts.code;
    this.payload = opts.payload;
    this.retryable = opts.retryable ?? isRetryableStatus(opts.status);
  }
}

export function isRetryableStatus(status: number): boolean {
  if (status === 0) return true; // network failure
  if (status === 408 || status === 425 || status === 429) return true;
  if (status >= 500 && status <= 599) return true;
  return false;
}

export interface RequestOptions extends Omit<RequestInit, "signal"> {
  /** Per-request timeout in ms. Defaults to 30s. Use 0 to disable. */
  timeoutMs?: number;
  /** External AbortSignal merged with the timeout signal. */
  signal?: AbortSignal;
}

const DEFAULT_TIMEOUT_MS = 30_000;

function buildHeaders(init?: HeadersInit): Headers {
  const headers = new Headers(init);
  if (!headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const key = getApiKey();
  if (key && !headers.has("X-API-Key")) {
    headers.set("X-API-Key", key);
  }
  return headers;
}

function makeAbortSignal(externalSignal: AbortSignal | undefined, timeoutMs: number): AbortSignal {
  if (timeoutMs <= 0) {
    return externalSignal ?? new AbortController().signal;
  }
  // AbortSignal.timeout / AbortSignal.any are widely supported in modern
  // browsers (Chrome ≥103, Firefox ≥114, Safari ≥17). Fall back to a manual
  // controller for older browsers.
  const timeoutSignal =
    typeof AbortSignal !== "undefined" && "timeout" in AbortSignal
      ? AbortSignal.timeout(timeoutMs)
      : timeoutFallback(timeoutMs);
  if (!externalSignal) return timeoutSignal;
  if (typeof AbortSignal !== "undefined" && "any" in AbortSignal) {
    return (AbortSignal as unknown as { any(s: AbortSignal[]): AbortSignal }).any([
      externalSignal,
      timeoutSignal,
    ]);
  }
  // Fallback: create a controller and abort on either signal.
  const controller = new AbortController();
  const onAbort = () => controller.abort();
  externalSignal.addEventListener("abort", onAbort, { once: true });
  timeoutSignal.addEventListener("abort", onAbort, { once: true });
  return controller.signal;
}

function timeoutFallback(timeoutMs: number): AbortSignal {
  const c = new AbortController();
  setTimeout(() => c.abort(new DOMException("timeout", "TimeoutError")), timeoutMs);
  return c.signal;
}

async function parseErrorPayload(response: Response): Promise<{
  message: string;
  code?: string;
  payload?: unknown;
}> {
  const text = await response.text().catch(() => "");
  if (!text) {
    return { message: response.statusText || `HTTP ${response.status}` };
  }
  try {
    const json = JSON.parse(text) as Record<string, unknown>;
    let message = "";
    let code: string | undefined;
    if (typeof json.message === "string") message = json.message;
    if (typeof json.error === "string") {
      code = json.error;
    } else if (json.error && typeof json.error === "object") {
      const inner = json.error as Record<string, unknown>;
      if (typeof inner.code === "string") code = inner.code;
      if (typeof inner.message === "string" && !message) message = inner.message;
    }
    if (typeof json.detail === "string" && !message) message = json.detail;
    return {
      message: message || response.statusText || `HTTP ${response.status}`,
      code,
      payload: json,
    };
  } catch {
    return { message: text };
  }
}

/**
 * httpRequest is the low-level building block. It returns a Response so
 * callers that need raw bodies (audio blobs, ZIP downloads) can read it
 * directly. JSON callers should prefer httpJson<T>.
 */
export async function httpRequest(path: string, options: RequestOptions = {}): Promise<Response> {
  const { timeoutMs = DEFAULT_TIMEOUT_MS, signal, headers, ...rest } = options;
  const response = await fetch(path, {
    ...rest,
    headers: buildHeaders(headers),
    signal: makeAbortSignal(signal, timeoutMs),
  }).catch((err) => {
    if (err && (err.name === "AbortError" || err.name === "TimeoutError")) {
      throw new ApiError({
        status: 0,
        code: err.name === "TimeoutError" ? "timeout" : "aborted",
        message: err.message || "request aborted",
        retryable: err.name === "TimeoutError",
      });
    }
    throw new ApiError({
      status: 0,
      code: "network",
      message: err instanceof Error ? err.message : "network error",
      retryable: true,
    });
  });

  if (!response.ok) {
    const parsed = await parseErrorPayload(response);
    throw new ApiError({
      status: response.status,
      code: parsed.code,
      message: parsed.message,
      payload: parsed.payload,
    });
  }
  return response;
}

export async function httpJson<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const response = await httpRequest(path, options);
  // 204 / 205 / DELETE responses may have no body.
  if (response.status === 204 || response.status === 205) {
    return undefined as T;
  }
  const text = await response.text();
  if (!text) return undefined as T;
  return JSON.parse(text) as T;
}
