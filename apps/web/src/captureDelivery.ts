import {
  ApiError,
  AuthenticationRequiredError,
  type CaptureAccepted,
} from "./api";
import type {
  CaptureQueueStore,
  PersistedCapture,
} from "./captureQueue";

const MAX_RETRY_DELAY_MS = 60_000;

export type CaptureDeliveryResult =
  | { kind: "empty" }
  | { kind: "waiting"; retryAt: number }
  | { kind: "delivered"; accepted: CaptureAccepted }
  | { kind: "retry"; retryAt: number; reason: string }
  | { kind: "blocked"; reason: string };

export type CaptureUploader = (
  capture: PersistedCapture,
) => Promise<CaptureAccepted>;

export function retryDelayMs(attempt: number): number {
  return Math.min(MAX_RETRY_DELAY_MS, 1_000 * (2 ** Math.max(0, attempt - 1)));
}

export function isRetryableCaptureError(error: unknown): boolean {
  if (error instanceof AuthenticationRequiredError) return true;
  if (!(error instanceof ApiError)) return true;
  if (
    error.status === 429
    && error.message.includes("trial_image_quota_exhausted")
  ) return false;
  return error.status === 408
    || error.status === 401
    || error.status === 425
    || error.status === 429
    || error.status >= 500;
}

function errorReason(error: unknown): string {
  return error instanceof Error ? error.message : "unknown upload error";
}

export async function deliverOldestCapture(
  store: CaptureQueueStore,
  upload: CaptureUploader,
  now: number,
): Promise<CaptureDeliveryResult> {
  const capture = await store.oldest();
  if (!capture) return { kind: "empty" };
  if (capture.status === "blocked") {
    return { kind: "blocked", reason: capture.lastError ?? "delivery is blocked" };
  }
  if (capture.nextAttemptAt > now) {
    return { kind: "waiting", retryAt: capture.nextAttemptAt };
  }

  try {
    const accepted = await upload(capture);
    await store.remove(capture.idempotencyKey);
    return { kind: "delivered", accepted };
  } catch (error: unknown) {
    const reason = errorReason(error);
    const attempts = capture.attempts + 1;
    if (!isRetryableCaptureError(error)) {
      await store.put({
        ...capture,
        attempts,
        status: "blocked",
        lastError: reason,
      });
      return { kind: "blocked", reason };
    }

    const retryAt = now + retryDelayMs(attempts);
    await store.put({
      ...capture,
      attempts,
      nextAttemptAt: retryAt,
      lastError: reason,
    });
    return { kind: "retry", retryAt, reason };
  }
}
