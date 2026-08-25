import "fake-indexeddb/auto";
import { describe, expect, it } from "vitest";
import {
  IndexedDbCaptureQueue,
  captureQueueDatabaseName,
  type PersistedCapture,
} from "./captureQueue";

function capture(
  idempotencyKey: string,
  createdAt: number,
): PersistedCapture {
  return {
    idempotencyKey,
    cameraId: "camera-1",
    image: new Blob(["jpeg"], { type: "image/jpeg" }),
    metadata: {
      capturedAt: new Date(createdAt).toISOString(),
      sequenceId: "browser-sequence-1",
      sequenceNumber: createdAt,
      width: 10,
      height: 20,
    },
    createdAt,
    attempts: 0,
    nextAttemptAt: createdAt,
    status: "pending",
  };
}

describe("IndexedDbCaptureQueue", () => {
  it("uses separate device databases for different authenticated owners", () => {
    expect(captureQueueDatabaseName("owner-one"))
      .not.toBe(captureQueueDatabaseName("owner-two"));
  });

  it("persists captures and returns them oldest-first across store instances", async () => {
    const databaseName = `capture-queue-${crypto.randomUUID()}`;
    const firstStore = new IndexedDbCaptureQueue(databaseName);
    await firstStore.add(capture("newer-frame", 200));
    await firstStore.add(capture("older-frame", 100));

    const reloadedStore = new IndexedDbCaptureQueue(databaseName);
    expect(await reloadedStore.count()).toBe(2);
    expect((await reloadedStore.oldest())?.idempotencyKey).toBe("older-frame");

    await reloadedStore.remove("older-frame");
    expect((await reloadedStore.oldest())?.idempotencyKey).toBe("newer-frame");
  });

  it("persists retry and blocked delivery state without losing image bytes", async () => {
    const store = new IndexedDbCaptureQueue(`capture-queue-${crypto.randomUUID()}`);
    const queued = capture("retry-frame", 100);
    await store.add(queued);
    await store.put({
      ...queued,
      attempts: 2,
      nextAttemptAt: 4_100,
      status: "blocked",
      lastError: "trial_image_quota_exhausted",
    });

    const stored = await store.oldest();
    expect(stored).toMatchObject({
      idempotencyKey: "retry-frame",
      attempts: 2,
      nextAttemptAt: 4_100,
      status: "blocked",
      lastError: "trial_image_quota_exhausted",
    });
    expect(await stored?.image.text()).toBe("jpeg");
  });
});
