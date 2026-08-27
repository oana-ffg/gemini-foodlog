import { describe, expect, it } from "vitest";
import {
  clearSignupLaunchMailIntent,
  readSignupLaunchMailIntent,
  saveSignupLaunchMailIntent,
} from "./signupIntent";

function memoryStorage() {
  const values = new Map<string, string>();
  return {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  };
}

describe("signup launch-mail intent", () => {
  it("keeps explicit checked and unchecked choices scoped to the Firebase user", () => {
    const storage = memoryStorage();

    saveSignupLaunchMailIntent("firebase-user-a", true, storage);
    saveSignupLaunchMailIntent("firebase-user-b", false, storage);

    expect(readSignupLaunchMailIntent("firebase-user-a", storage)).toBe(true);
    expect(readSignupLaunchMailIntent("firebase-user-b", storage)).toBe(false);
    expect(readSignupLaunchMailIntent("firebase-user-c", storage)).toBeUndefined();
  });

  it("clears the local handoff only after the server decision is durable", () => {
    const storage = memoryStorage();
    saveSignupLaunchMailIntent("firebase-user-a", true, storage);

    clearSignupLaunchMailIntent("firebase-user-a", storage);

    expect(readSignupLaunchMailIntent("firebase-user-a", storage)).toBeUndefined();
  });

  it("does not break account creation when browser storage is unavailable", () => {
    const unavailableStorage = {
      getItem: () => { throw new Error("storage unavailable"); },
      setItem: () => { throw new Error("storage unavailable"); },
      removeItem: () => { throw new Error("storage unavailable"); },
    };

    expect(() => saveSignupLaunchMailIntent(
      "firebase-user-a",
      true,
      unavailableStorage,
    )).not.toThrow();
    expect(readSignupLaunchMailIntent("firebase-user-a", unavailableStorage)).toBeUndefined();
    expect(() => clearSignupLaunchMailIntent(
      "firebase-user-a",
      unavailableStorage,
    )).not.toThrow();
  });
});
