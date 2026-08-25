import "fake-indexeddb/auto";
import { describe, expect, it, vi } from "vitest";
import { ApiError, type CaptureAccepted } from "./api";
import {
  deliverOldestCapture,
  isRetryableCaptureError,
  retryDelayMs,
} from "./captureDelivery";
import type {
  CaptureQueueStore,
  PersistedCapture,
} from "./captureQueue";
import { IndexedDbCaptureQueue } from "./captureQueue";

const accepted: CaptureAccepted = {
  capture_id: "capture-1",
  accepted_image_count: 1,
  entitlement_mode: "trial",
  trial_image_limit: 200,
  duplicate: false,
};

function queuedCapture(): PersistedCapture {
  return {
    idempotencyKey: "idempotency-1",
    cameraId: "camera-1",
    image: new Blob(["jpeg"], { type: "image/jpeg" }),
    metadata: {
      capturedAt: "2026-08-25T20:00:00Z",
      sequenceId: "browser-sequence-1",
      sequenceNumber: 1,
      width: 10,
      height: 20,
    },
    createdAt: 100,
    attempts: 0,
    nextAttemptAt: 100,
    status: "pending",
  };
}

class MemoryStore implements CaptureQueueStore {
  capture: PersistedCapture | undefined = queuedCapture();

  async add(capture: PersistedCapture) {
    this.capture = capture;
  }

  async count() {
    return this.capture ? 1 : 0;
  }

  async oldest() {
    return this.capture;
  }

  async put(capture: PersistedCapture) {
    this.capture = capture;
  }

  async remove() {
    this.capture = undefined;
  }
}

describe("deliverOldestCapture", () => {
  it("backs off retryable failures and recovers without changing idempotency", async () => {
    const store = new MemoryStore();
    const upload = vi.fn()
      .mockRejectedValueOnce(new TypeError("offline"))
      .mockResolvedValueOnce(accepted);

    await expect(deliverOldestCapture(store, upload, 1_000)).resolves.toEqual({
      kind: "retry",
      retryAt: 2_000,
      reason: "offline",
    });
    expect(store.capture).toMatchObject({
      idempotencyKey: "idempotency-1",
      attempts: 1,
      nextAttemptAt: 2_000,
      status: "pending",
    });

    await expect(deliverOldestCapture(store, upload, 1_999)).resolves.toEqual({
      kind: "waiting",
      retryAt: 2_000,
    });
    expect(upload).toHaveBeenCalledTimes(1);

    await expect(deliverOldestCapture(store, upload, 2_000)).resolves.toEqual({
      kind: "delivered",
      accepted,
    });
    expect(upload.mock.calls.map(([capture]) => capture.idempotencyKey))
      .toEqual(["idempotency-1", "idempotency-1"]);
    expect(store.capture).toBeUndefined();
  });

  it("blocks quota exhaustion instead of retrying forever", async () => {
    const store = new MemoryStore();
    const upload = vi.fn().mockRejectedValue(
      new ApiError("trial_image_quota_exhausted", 429),
    );

    await expect(deliverOldestCapture(store, upload, 1_000)).resolves.toEqual({
      kind: "blocked",
      reason: "trial_image_quota_exhausted",
    });
    expect(store.capture).toMatchObject({
      attempts: 1,
      status: "blocked",
      lastError: "trial_image_quota_exhausted",
    });
  });
});

describe("capture delivery policy", () => {
  it("uses bounded exponential delays and distinguishes temporary 429s", () => {
    expect([1, 2, 3, 8].map(retryDelayMs)).toEqual([1_000, 2_000, 4_000, 60_000]);
    expect(isRetryableCaptureError(new ApiError("busy", 429))).toBe(true);
    expect(isRetryableCaptureError(new ApiError("invalid_capture_metadata", 422))).toBe(false);
  });
});

describe("persistent offline reload recovery", () => {
  it("keeps exact FIFO order and idempotency after a reload and transient outage", async () => {
    const databaseName = `capture-delivery-${crypto.randomUUID()}`;
    const beforeReload = new IndexedDbCaptureQueue(databaseName);
    const newer = { ...queuedCapture(), idempotencyKey: "newer", createdAt: 200 };
    const older = { ...queuedCapture(), idempotencyKey: "older", createdAt: 100 };
    await beforeReload.add(newer);
    await beforeReload.add(older);

    const afterReload = new IndexedDbCaptureQueue(databaseName);
    const deliveredIds: string[] = [];
    const upload = vi.fn(async (capture: PersistedCapture) => {
      deliveredIds.push(capture.idempotencyKey);
      if (deliveredIds.length === 1) throw new TypeError("offline");
      return accepted;
    });

    await expect(deliverOldestCapture(afterReload, upload, 1_000))
      .resolves.toMatchObject({ kind: "retry", retryAt: 2_000 });
    await expect(deliverOldestCapture(afterReload, upload, 2_000))
      .resolves.toMatchObject({ kind: "delivered" });
    await expect(deliverOldestCapture(afterReload, upload, 2_000))
      .resolves.toMatchObject({ kind: "delivered" });

    expect(deliveredIds).toEqual(["older", "older", "newer"]);
    expect(await afterReload.count()).toBe(0);
  });
});
