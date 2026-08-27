const SIGNUP_INTENT_PREFIX = "foodlog:v1:signup-launch-mail:";

interface SignupIntentStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

function intentKey(firebaseUid: string): string {
  return `${SIGNUP_INTENT_PREFIX}${firebaseUid}`;
}

export function saveSignupLaunchMailIntent(
  firebaseUid: string,
  optedIn: boolean,
  storage: SignupIntentStorage = window.localStorage,
): void {
  try {
    storage.setItem(intentKey(firebaseUid), optedIn ? "opted-in" : "opted-out");
  } catch {
    // Storage can be unavailable in privacy modes. Signup must still succeed;
    // the user can set the same preference from the authenticated account UI.
  }
}

export function readSignupLaunchMailIntent(
  firebaseUid: string,
  storage: SignupIntentStorage = window.localStorage,
): boolean | undefined {
  let value: string | null;
  try {
    value = storage.getItem(intentKey(firebaseUid));
  } catch {
    return undefined;
  }
  if (value === "opted-in") return true;
  if (value === "opted-out") return false;
  return undefined;
}

export function clearSignupLaunchMailIntent(
  firebaseUid: string,
  storage: SignupIntentStorage = window.localStorage,
): void {
  try {
    storage.removeItem(intentKey(firebaseUid));
  } catch {
    // A failed cleanup cannot invalidate the durable server-side preference.
  }
}
