import { describe, expect, it } from "vitest";

import {
  browserCameraDisplayName,
  browserCameraInstanceId,
  replaceBrowserCameraInstanceId,
} from "./browserCameraIdentity";

class MemoryStorage {
  readonly values = new Map<string, string>();

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }
}

describe("browserCameraInstanceId", () => {
  it("persists one opaque identity for this browser installation", () => {
    const storage = new MemoryStorage();
    const generated = browserCameraInstanceId(
      storage,
      () => "11111111-1111-4111-8111-111111111111",
    );
    const restored = browserCameraInstanceId(
      storage,
      () => "22222222-2222-4222-8222-222222222222",
    );

    expect(generated).toBe("browser-11111111-1111-4111-8111-111111111111");
    expect(restored).toBe(generated);
  });

  it("replaces malformed persisted values", () => {
    const storage = new MemoryStorage();
    storage.setItem("foodlog.browser-camera-instance.v1", "not-a-camera-identity");

    expect(browserCameraInstanceId(
      storage,
      () => "33333333-3333-4333-8333-333333333333",
    )).toMatch(/^browser-[0-9a-f-]{36}$/);
  });

  it("keeps an in-tab identity when storage is unavailable", () => {
    const unavailableStorage = {
      getItem: () => { throw new Error("storage blocked"); },
      setItem: () => { throw new Error("storage blocked"); },
    };

    const first = browserCameraInstanceId(
      unavailableStorage,
      () => "44444444-4444-4444-8444-444444444444",
    );
    const second = browserCameraInstanceId(
      unavailableStorage,
      () => "55555555-5555-4555-8555-555555555555",
    );
    expect(second).toBe(first);
  });

  it("rotates the persistent identity after this browser source is revoked", () => {
    const storage = new MemoryStorage();
    const original = browserCameraInstanceId(
      storage,
      () => "66666666-6666-4666-8666-666666666666",
    );
    const replacement = replaceBrowserCameraInstanceId(
      storage,
      () => "77777777-7777-4777-8777-777777777777",
    );

    expect(replacement).not.toBe(original);
    expect(browserCameraInstanceId(storage)).toBe(replacement);
  });

  it("derives a stable non-blocking label from the opaque browser identity", () => {
    expect(browserCameraDisplayName(
      "browser-88888888-8888-4888-8888-123456789abc",
    )).toBe("Browser camera 56789abc");
    expect(browserCameraDisplayName("invalid")).toBe("Browser camera");
  });
});
