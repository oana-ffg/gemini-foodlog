const STORAGE_KEY = "foodlog.browser-camera-instance.v1";
const INSTANCE_PATTERN = /^browser-[0-9a-f-]{36}$/;

let volatileInstanceId: string | undefined;

export function browserCameraInstanceId(
  storage: Pick<Storage, "getItem" | "setItem"> = window.localStorage,
  randomUuid: () => string = () => crypto.randomUUID(),
): string {
  try {
    const stored = storage.getItem(STORAGE_KEY);
    if (stored && INSTANCE_PATTERN.test(stored)) return stored;
    const generated = `browser-${randomUuid()}`;
    storage.setItem(STORAGE_KEY, generated);
    return generated;
  } catch {
    // Browsers may deny storage in strict privacy modes. A stable in-tab ID still
    // prevents duplicate registration while this page remains open.
  }

  if (!volatileInstanceId) volatileInstanceId = `browser-${randomUuid()}`;
  return volatileInstanceId;
}
