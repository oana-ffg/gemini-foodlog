import type { PasswordValidationStatus } from "firebase/auth";

type PasswordCriteria = Pick<
  PasswordValidationStatus,
  | "containsLowercaseLetter"
  | "containsNonAlphanumericCharacter"
  | "containsNumericCharacter"
  | "containsUppercaseLetter"
  | "meetsMaxPasswordLength"
  | "meetsMinPasswordLength"
  | "passwordPolicy"
>;

export function passwordPolicyErrorMessage(status: PasswordCriteria): string {
  const missing: string[] = [];
  const options = status.passwordPolicy.customStrengthOptions;

  if (status.meetsMinPasswordLength === false) {
    missing.push(
      options.minPasswordLength === undefined
        ? "the required minimum length"
        : `at least ${options.minPasswordLength} characters`,
    );
  }
  if (status.meetsMaxPasswordLength === false && options.maxPasswordLength !== undefined) {
    missing.push(`at most ${options.maxPasswordLength} characters`);
  }
  if (status.containsLowercaseLetter === false) missing.push("a lowercase letter");
  if (status.containsUppercaseLetter === false) missing.push("an uppercase letter");
  if (status.containsNumericCharacter === false) missing.push("a number");
  if (status.containsNonAlphanumericCharacter === false) {
    missing.push("a non-alphanumeric character");
  }

  return missing.length
    ? `Your password needs ${missing.join(", ")}.`
    : "Your password does not meet the current security policy.";
}

export function minimumPasswordLength(status: PasswordCriteria): number | undefined {
  return status.passwordPolicy.customStrengthOptions.minPasswordLength;
}

export async function createAfterPasswordValidation<T>(
  validate: () => Promise<PasswordValidationStatus>,
  create: () => Promise<T>,
): Promise<T> {
  let status: PasswordValidationStatus | undefined;
  try {
    status = await validate();
  } catch {
    // Identity Platform is authoritative; a client policy-fetch failure must not
    // make signup less available than the server-enforced account-creation call.
  }
  if (status && !status.isValid) {
    throw new Error(passwordPolicyErrorMessage(status));
  }
  return create();
}
