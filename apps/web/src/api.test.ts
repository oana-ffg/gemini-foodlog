import { afterEach, describe, expect, it, vi } from "vitest";

const firebase = vi.hoisted(() => ({
  auth: {
    currentUser: null as null | { getIdToken: (forceRefresh?: boolean) => Promise<string> },
  },
}));

vi.mock("./firebase", () => firebase);

import {
  AuthenticationRequiredError,
  correctKnowledge,
  createContextNote,
  createDeviceCamera,
  getKnowledgePage,
  getConsentPreferences,
  joinWaitlist,
  listActivities,
  listCameras,
  listContextNotes,
  listKnowledge,
  listProcessing,
  listPurchases,
  provisionAccount,
  recordLaunchMailConsent,
  retireKnowledge,
  retireContextNote,
  revokeCamera,
  submitMealFeedback,
  teachKnowledge,
  uploadCapture,
  withdrawLaunchMailConsent,
  withdrawWaitlist,
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

  it("surfaces a stale session after the one allowed token refresh also fails", async () => {
    const getIdToken = vi.fn()
      .mockResolvedValueOnce("expired-token")
      .mockResolvedValueOnce("rejected-refresh-token");
    firebase.auth.currentUser = { getIdToken };
    const fetchMock = vi.fn()
      .mockResolvedValue(jsonResponse({ detail: "invalid_authentication" }, 401));
    vi.stubGlobal("fetch", fetchMock);

    await expect(provisionAccount()).rejects.toBeInstanceOf(AuthenticationRequiredError);

    expect(getIdToken.mock.calls).toEqual([[false], [true]]);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("loads owner processing and purchase context from explicit bounded routes", async () => {
    firebase.auth.currentUser = {
      getIdToken: vi.fn().mockResolvedValue("firebase-id-token"),
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse([]))
      .mockResolvedValueOnce(jsonResponse([]));
    vi.stubGlobal("fetch", fetchMock);

    await listProcessing(12);
    await listPurchases(1);

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "http://127.0.0.1:8080/v1/processing?limit=12",
      "http://127.0.0.1:8080/v1/purchases?limit=1",
    ]);
  });

  it("loads complete activity history and sends revision-bound targeted correction", async () => {
    firebase.auth.currentUser = {
      getIdToken: vi.fn().mockResolvedValue("firebase-id-token"),
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse([]))
      .mockResolvedValueOnce(jsonResponse({}));
    vi.stubGlobal("fetch", fetchMock);

    await listActivities("not_cooking");
    await submitMealFeedback("meal/one", {
      kind: "correct",
      correction: {
        scope: "component",
        component_index: 1,
        replacement: {
          name: "Duck breast",
          ingredients: ["duck"],
          preparation_methods: ["air frying"],
        },
      },
      base_revision_number: 4,
      explanation: "The darker red meat and recent duck purchase distinguish it.",
      learning_disposition: "reusable",
    }, "feedback-key-0001");

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "http://127.0.0.1:8080/v1/activities?status=not_cooking",
      "http://127.0.0.1:8080/v1/meals/meal%2Fone/feedback",
    ]);
    const feedbackInit = fetchMock.mock.calls[1][1] as RequestInit;
    expect(feedbackInit.method).toBe("POST");
    expect(new Headers(feedbackInit.headers).get("Idempotency-Key")).toBe(
      "feedback-key-0001",
    );
    expect(feedbackInit.body).toBe(JSON.stringify({
      kind: "correct",
      correction: {
        scope: "component",
        component_index: 1,
        replacement: {
          name: "Duck breast",
          ingredients: ["duck"],
          preparation_methods: ["air frying"],
        },
      },
      base_revision_number: 4,
      explanation: "The darker red meat and recent duck purchase distinguish it.",
      learning_disposition: "reusable",
    }));
  });

  it("uses explicit authenticated consent and waitlist operations", async () => {
    firebase.auth.currentUser = {
      getIdToken: vi.fn().mockResolvedValue("firebase-id-token"),
    };
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (url.endsWith("/v1/consents")) {
        return Promise.resolve(jsonResponse({
          launch_mail_opt_in: false,
          launch_mail_policy_version: "launch-interest-v1",
          launch_mail_updated_at: "2026-08-27T12:00:00Z",
          waitlist_status: "not_joined",
          waitlist_policy_version: null,
          waitlist_updated_at: null,
        }));
      }
      if (url.includes("launch-mail")) {
        return Promise.resolve(jsonResponse({
          id: "consent-1",
          granted: false,
          policy_version: "launch-interest-v1",
          created_at: "2026-08-27T12:00:00Z",
        }));
      }
      return Promise.resolve(jsonResponse({
        id: "waitlist-1",
        email_normalized: null,
        policy_version: "capacity-waitlist-v1",
        mailing_list_opt_in: false,
        status: "withdrawn",
        updated_at: "2026-08-27T12:00:00Z",
        last_withdrawn_at: "2026-08-27T12:00:00Z",
        withdrawal_count: 1,
      }));
    });
    vi.stubGlobal("fetch", fetchMock);

    await getConsentPreferences();
    await recordLaunchMailConsent(true);
    await withdrawLaunchMailConsent();
    await joinWaitlist();
    await withdrawWaitlist();

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "http://127.0.0.1:8080/v1/consents",
      "http://127.0.0.1:8080/v1/consents/launch-mail",
      "http://127.0.0.1:8080/v1/consents/launch-mail/withdraw",
      "http://127.0.0.1:8080/v1/waitlist",
      "http://127.0.0.1:8080/v1/waitlist/withdraw",
    ]);
    const recordInit = fetchMock.mock.calls[1][1] as RequestInit;
    const joinInit = fetchMock.mock.calls[3][1] as RequestInit;
    expect(recordInit.method).toBe("POST");
    expect(recordInit.body).toBe(JSON.stringify({ granted: true }));
    expect(joinInit.method).toBe("POST");
    expect(joinInit.body).toBe(JSON.stringify({ join: true }));
  });

  it("uses the owner-scoped camera inventory, issue, and revocation routes", async () => {
    firebase.auth.currentUser = {
      getIdToken: vi.fn().mockResolvedValue("firebase-id-token"),
    };
    const browserCamera = {
      id: "browser-camera-1",
      account_id: "account-1",
      name: "Phone by sink",
      kind: "browser" as const,
      status: "active" as const,
      accepted_capture_count: 3,
      last_capture_at: "2026-08-27T12:00:00Z",
      created_at: "2026-08-27T10:00:00Z",
      revoked_at: null,
    };
    const deviceCamera = {
      ...browserCamera,
      id: "device-camera-1",
      name: "ESP kitchen camera",
      kind: "device" as const,
      accepted_capture_count: 0,
      last_capture_at: null,
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(jsonResponse([browserCamera]))
      .mockResolvedValueOnce(jsonResponse({
        camera: deviceCamera,
        credential: "flc_v1_single-display-secret",
      }))
      .mockResolvedValueOnce(jsonResponse({
        ...browserCamera,
        status: "revoked",
        revoked_at: "2026-08-27T13:00:00Z",
      }));
    vi.stubGlobal("fetch", fetchMock);

    await expect(listCameras()).resolves.toEqual([browserCamera]);
    await expect(createDeviceCamera("ESP kitchen camera")).resolves.toEqual({
      camera: deviceCamera,
      credential: "flc_v1_single-display-secret",
    });
    await revokeCamera("browser camera/1");

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "http://127.0.0.1:8080/v1/cameras",
      "http://127.0.0.1:8080/v1/device-cameras",
      "http://127.0.0.1:8080/v1/cameras/browser%20camera%2F1/revoke",
    ]);
    expect((fetchMock.mock.calls[1][1] as RequestInit).body).toBe(
      JSON.stringify({ name: "ESP kitchen camera" }),
    );
  });

  it("uses the authenticated household-wiki CRUD and history routes", async () => {
    firebase.auth.currentUser = {
      getIdToken: vi.fn().mockResolvedValue("firebase-id-token"),
    };
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse({})));
    vi.stubGlobal("fetch", fetchMock);

    await listKnowledge(true);
    await getKnowledgePage("page/one");
    await teachKnowledge("The sink-side basket usually means steak.", "teach-key-0001");
    await correctKnowledge("page/one", "It can also mean chicken.", 2, "correct-key-0001");
    await retireKnowledge("page/one", 3, "No longer reliable.", "retire-key-0001");

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "http://127.0.0.1:8080/v1/knowledge?include_retired=true",
      "http://127.0.0.1:8080/v1/knowledge/page%2Fone",
      "http://127.0.0.1:8080/v1/knowledge",
      "http://127.0.0.1:8080/v1/knowledge/page%2Fone/correct",
      "http://127.0.0.1:8080/v1/knowledge/page%2Fone/retire",
    ]);
    const teachInit = fetchMock.mock.calls[2][1] as RequestInit;
    const correctInit = fetchMock.mock.calls[3][1] as RequestInit;
    const retireInit = fetchMock.mock.calls[4][1] as RequestInit;
    expect(new Headers(teachInit.headers).get("Idempotency-Key")).toBe("teach-key-0001");
    expect(teachInit.body).toBe(JSON.stringify({
      statement: "The sink-side basket usually means steak.",
    }));
    expect(correctInit.body).toBe(JSON.stringify({
      statement: "It can also mean chicken.",
      expected_revision_number: 2,
    }));
    expect(retireInit.body).toBe(JSON.stringify({
      expected_revision_number: 3,
      reason: "No longer reliable.",
    }));
  });

  it("uses immutable context-note create, history, and retirement routes", async () => {
    firebase.auth.currentUser = {
      getIdToken: vi.fn().mockResolvedValue("firebase-id-token"),
    };
    const fetchMock = vi.fn().mockImplementation(() => Promise.resolve(jsonResponse({})));
    vi.stubGlobal("fetch", fetchMock);

    await createContextNote({
      text: "My MIL brought duck for tomorrow.",
      valid_from: "2026-08-28T00:00:00Z",
      valid_until: "2026-08-29T00:00:00Z",
    }, "context-key-0001");
    await listContextNotes(true);
    await retireContextNote("note/one");

    expect(fetchMock.mock.calls.map(([url]) => url)).toEqual([
      "http://127.0.0.1:8080/v1/context-notes",
      "http://127.0.0.1:8080/v1/context-notes?include_inactive=true",
      "http://127.0.0.1:8080/v1/context-notes/note%2Fone/retire",
    ]);
    const createInit = fetchMock.mock.calls[0][1] as RequestInit;
    expect(new Headers(createInit.headers).get("Idempotency-Key")).toBe("context-key-0001");
    expect(createInit.body).toBe(JSON.stringify({
      text: "My MIL brought duck for tomorrow.",
      valid_from: "2026-08-28T00:00:00Z",
      valid_until: "2026-08-29T00:00:00Z",
    }));
    expect((fetchMock.mock.calls[2][1] as RequestInit).method).toBe("POST");
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
        burstId: "motion-burst-1",
        burstFrameIndex: 3,
        motion: {
          detected: true,
          algorithm: "browser-luma-delta-v1",
          score: 0.24,
          changedPixelRatio: 0.18,
          threshold: 0.03,
        },
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
      burst_id: "motion-burst-1",
      burst_frame_index: 3,
      width: 1280,
      height: 720,
      motion: {
        detected: true,
        algorithm: "browser-luma-delta-v1",
        score: 0.24,
        changed_pixel_ratio: 0.18,
        threshold: 0.03,
      },
    });
    expect(form.get("image")).toBeInstanceOf(Blob);
  });

  it("refuses incomplete motion burst metadata before the network", async () => {
    firebase.auth.currentUser = {
      getIdToken: vi.fn().mockResolvedValue("firebase-id-token"),
    };
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    expect(() => uploadCapture(
        "camera-1",
        new Blob(["jpeg bytes"], { type: "image/jpeg" }),
        "idempotency-1",
        {
          capturedAt: "2026-08-25T15:00:00.000Z",
          sequenceId: "browser-sequence-1",
          sequenceNumber: 7,
          width: 1280,
          height: 720,
          burstId: "motion-burst-1",
        },
      ))
      .toThrow("frame index");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
