import { describe, expect, it, vi } from "vitest";
import {
  CaptureWakeLockController,
  type CaptureWakeLockStatus,
  type VisibilityDocument,
  type WakeLockSentinelLike,
} from "./wakeLock";

class FakeVisibility implements VisibilityDocument {
  visibilityState: DocumentVisibilityState = "visible";
  listener: (() => void) | undefined;

  addEventListener(_: "visibilitychange", listener: () => void) {
    this.listener = listener;
  }

  removeEventListener(_: "visibilitychange", listener: () => void) {
    if (this.listener === listener) this.listener = undefined;
  }

  change(state: DocumentVisibilityState) {
    this.visibilityState = state;
    this.listener?.();
  }
}

function sentinel(): WakeLockSentinelLike & { releaseListener?: () => void } {
  return {
    released: false,
    release: vi.fn(async function (this: { released: boolean }) {
      this.released = true;
    }),
    addEventListener(_: "release", listener: () => void) {
      this.releaseListener = listener;
    },
  };
}

async function nextTask() {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

describe("CaptureWakeLockController", () => {
  it("acquires only while requested and releases on stop", async () => {
    const lock = sentinel();
    const statuses: CaptureWakeLockStatus[] = [];
    const controller = new CaptureWakeLockController(
      { request: vi.fn().mockResolvedValue(lock) },
      new FakeVisibility(),
      (status) => statuses.push(status),
    );

    controller.start();
    await nextTask();
    expect(statuses).toEqual(["requesting", "active"]);

    controller.stop();
    await nextTask();
    expect(lock.release).toHaveBeenCalledOnce();
    expect(statuses.at(-1)).toBe("inactive");
  });

  it("releases while hidden and reacquires when visible", async () => {
    const first = sentinel();
    const second = sentinel();
    const visibility = new FakeVisibility();
    const statuses: CaptureWakeLockStatus[] = [];
    const request = vi.fn()
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(second);
    const controller = new CaptureWakeLockController(
      { request },
      visibility,
      (status) => statuses.push(status),
    );

    controller.start();
    await nextTask();
    visibility.change("hidden");
    await nextTask();
    expect(first.release).toHaveBeenCalledOnce();
    expect(statuses.at(-1)).toBe("hidden");

    visibility.change("visible");
    await nextTask();
    expect(request).toHaveBeenCalledTimes(2);
    expect(statuses.at(-1)).toBe("active");
  });

  it("reports unsupported and denied states without claiming an active lock", async () => {
    const visibility = new FakeVisibility();
    const unsupported: CaptureWakeLockStatus[] = [];
    new CaptureWakeLockController(
      undefined,
      visibility,
      (status) => unsupported.push(status),
    ).start();
    expect(unsupported).toEqual(["unsupported"]);

    const denied: CaptureWakeLockStatus[] = [];
    new CaptureWakeLockController(
      { request: vi.fn().mockRejectedValue(new Error("denied")) },
      visibility,
      (status) => denied.push(status),
    ).start();
    await nextTask();
    expect(denied).toEqual(["requesting", "denied"]);
  });
});
