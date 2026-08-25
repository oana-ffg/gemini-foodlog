import { afterEach, describe, expect, it, vi } from "vitest";

const firebase = vi.hoisted(() => ({
  auth: {
    currentUser: null as null | { getIdToken: (forceRefresh?: boolean) => Promise<string> },
  },
}));

vi.mock("./firebase", () => firebase);

import {
  AuthenticationRequiredError,
  provisionAccount,
  uploadCapture,
} from "./api";

const account = {
  id: "account-1",
  owner_user_id: "firebase-user-1",
  entitlement_mode: "trial",
  trial_image_limit: 200,
  accepted_image_count: 0,
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

afterEach(() => {
  firebase.auth.currentUser = null;
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("authenticated API client", () => {
  it("fails before making a request when the user is signed out", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    await expect(provisionAccount()).rejects.toBeInstanceOf(AuthenticationRequiredError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("uses the current Firebase ID token and cannot be downgraded to local identity", async () => {
    const getIdToken = vi.fn().mockResolvedValue("firebase-id-token");
    firebase.auth.currentUser = { getIdToken };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(account));
    vi.stubGlobal("fetch", fetchMock);

    await expect(provisionAccount()).resolves.toEqual(account);

    expect(getIdToken).toHaveBeenCalledWith(false);
    expect(fetchMock).toHaveBeenCalledOnce();
    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    const headers = new Headers(init.headers);
    expect(headers.get("Authorization")).toBe("Bearer firebase-id-token");
    expect(headers.has("X-FoodLog-Local-User")).toBe(false);
  });

  it("forces one token refresh after an expired-token response", async () => {
    const getIdToken = vi.fn()
      .mockResolvedValueOnce("expired-token")
      .mockResolvedValueOnce("refreshed-token");
    firebase.auth.currentUser = { getIdToken };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse({ detail: "invalid_authentication" }, 401))
      .mockResolvedValueOnce(jsonResponse(account));
    vi.stubGlobal("fetch", fetchMock);

    await expect(provisionAccount()).resolves.toEqual(account);

    expect(getIdToken.mock.calls).toEqual([[false], [true]]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    const firstHeaders = new Headers((fetchMock.mock.calls[0][1] as RequestInit).headers);
    const secondHeaders = new Headers((fetchMock.mock.calls[1][1] as RequestInit).headers);
    expect(firstHeaders.get("Authorization")).toBe("Bearer expired-token");
    expect(secondHeaders.get("Authorization")).toBe("Bearer refreshed-token");
  });

  it("uploads browser snapshots through the shared capture envelope", async () => {
    firebase.auth.currentUser = {
      getIdToken: vi.fn().mockResolvedValue("firebase-id-token"),
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({
      capture_id: "capture-1",
      accepted_image_count: 1,
      entitlement_mode: "trial",
      trial_image_limit: 200,
      duplicate: false,
    }, 202));
    vi.stubGlobal("fetch", fetchMock);

    await uploadCapture(
      "camera-1",
      new Blob(["jpeg bytes"], { type: "image/jpeg" }),
      "idempotency-1",
      {
        capturedAt: "2026-08-25T15:00:00.000Z",
        sequenceId: "browser-sequence-1",
        sequenceNumber: 7,
        width: 1280,
        height: 720,
      },
    );

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("http://127.0.0.1:8080/v1/captures");
    expect(new Headers(init.headers).get("Idempotency-Key")).toBe("idempotency-1");
    const form = init.body as FormData;
    expect(JSON.parse(form.get("metadata") as string)).toEqual({
      schema_version: 1,
      camera_id: "camera-1",
      captured_at: "2026-08-25T15:00:00.000Z",
      client_kind: "browser",
      client_version: "web-mvp/1",
      sequence_id: "browser-sequence-1",
      sequence_number: 7,
      width: 1280,
      height: 720,
    });
    expect(form.get("image")).toBeInstanceOf(Blob);
  });
});
