import { describe, expect, it, vi } from "vitest";

import {
  createAfterPasswordValidation,
  minimumPasswordLength,
  passwordPolicyErrorMessage,
} from "./passwordPolicy";

const policy = {
  customStrengthOptions: {
    minPasswordLength: 12,
    containsUppercaseLetter: false,
    containsNumericCharacter: false,
  },
  enforcementState: "ENFORCE",
  forceUpgradeOnSignin: false,
  allowedNonAlphanumericCharacters: "",
};

describe("signup password policy", () => {
  it("reads the current minimum from Firebase instead of duplicating it", () => {
    expect(minimumPasswordLength({
      meetsMinPasswordLength: false,
      meetsMaxPasswordLength: undefined,
      containsLowercaseLetter: undefined,
      containsUppercaseLetter: undefined,
      containsNumericCharacter: undefined,
      containsNonAlphanumericCharacter: undefined,
      passwordPolicy: policy,
    })).toBe(12);
  });

  it("explains the exact unmet Firebase criteria", () => {
    expect(passwordPolicyErrorMessage({
      meetsMinPasswordLength: false,
      meetsMaxPasswordLength: undefined,
      containsLowercaseLetter: undefined,
      containsUppercaseLetter: false,
      containsNumericCharacter: false,
      containsNonAlphanumericCharacter: undefined,
      passwordPolicy: policy,
    })).toBe("Your password needs at least 12 characters, an uppercase letter, a number.");
  });

  it("fails closed when Firebase reports an invalid policy without criteria", () => {
    expect(passwordPolicyErrorMessage({
      meetsMinPasswordLength: undefined,
      meetsMaxPasswordLength: undefined,
      containsLowercaseLetter: undefined,
      containsUppercaseLetter: undefined,
      containsNumericCharacter: undefined,
      containsNonAlphanumericCharacter: undefined,
      passwordPolicy: policy,
    })).toBe("Your password does not meet the current security policy.");
  });

  it("does not create an account after a confirmed invalid result", async () => {
    const create = vi.fn(async () => "created");

    await expect(createAfterPasswordValidation(
      async () => ({
        isValid: false,
        meetsMinPasswordLength: false,
        meetsMaxPasswordLength: undefined,
        containsLowercaseLetter: undefined,
        containsUppercaseLetter: undefined,
        containsNumericCharacter: undefined,
        containsNonAlphanumericCharacter: undefined,
        passwordPolicy: policy,
      }),
      create,
    )).rejects.toThrow("at least 12 characters");
    expect(create).not.toHaveBeenCalled();
  });

  it("creates after a valid result or a client policy-fetch failure", async () => {
    const create = vi.fn(async () => "created");

    await expect(createAfterPasswordValidation(
      async () => ({
        isValid: true,
        meetsMinPasswordLength: true,
        meetsMaxPasswordLength: undefined,
        containsLowercaseLetter: undefined,
        containsUppercaseLetter: undefined,
        containsNumericCharacter: undefined,
        containsNonAlphanumericCharacter: undefined,
        passwordPolicy: policy,
      }),
      create,
    )).resolves.toBe("created");
    await expect(createAfterPasswordValidation(
      async () => { throw new Error("policy fetch unavailable"); },
      create,
    )).resolves.toBe("created");
    expect(create).toHaveBeenCalledTimes(2);
  });
});
