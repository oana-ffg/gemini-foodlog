import { auth } from "./firebase";

export type Confidence = "confident" | "likely" | "uncertain";
export type MealStatus = "provisional" | "confirmed" | "corrected" | "contradicted";
export type MealFeedbackKind = "confirm" | "correct";
export type MealRevisionSource = "inference" | "user_feedback";

export interface Account {
  id: string;
  owner_user_id: string;
  entitlement_mode: "trial" | "unlimited";
  trial_image_limit: number | null;
  accepted_image_count: number;
}

export interface BrowserCamera {
  id: string;
  account_id: string;
  name: string;
  kind: "browser";
}

export interface MealComponent {
  name: string;
  ingredients: string[];
  preparation_methods: string[];
}

export interface MealEntry {
  id: string;
  account_id: string;
  capture_id: string;
  status: MealStatus;
  confidence: Confidence;
  title: string;
  components: MealComponent[];
  observations: string[];
  alternatives: string[];
  rationale: string;
  clarification_question: string | null;
  clarification_reason: string | null;
  revision_number: number;
  created_at: string;
}

export interface MealRevision {
  id: string;
  meal_id: string;
  number: number;
  status: MealStatus;
  source: MealRevisionSource;
  inference: Pick<
    MealEntry,
    | "title"
    | "confidence"
    | "components"
    | "observations"
    | "alternatives"
    | "rationale"
    | "clarification_question"
    | "clarification_reason"
  >;
  created_at: string;
}

export interface MealFeedbackInput {
  kind: MealFeedbackKind;
  actual_meal?: string;
  explanation?: string;
}

export interface MealFeedbackResult {
  revision: MealRevision;
}

export interface ClarificationQuestion {
  id: string;
  meal_id: string;
  prompt: string;
  reason: string;
  status: "open" | "answered";
  answer: string | null;
  learning_tip: string | null;
  created_at: string;
}

export interface QuestionAnswerResult {
  question: ClarificationQuestion;
  revision: MealRevision;
}

export interface CaptureAccepted {
  capture_id: string;
  accepted_image_count: number;
  entitlement_mode: "trial" | "unlimited";
  trial_image_limit: number | null;
  duplicate: boolean;
}

export interface BrowserCaptureMetadata {
  capturedAt: string;
  sequenceId: string;
  sequenceNumber: number;
  width: number;
  height: number;
}

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8080";

export class AuthenticationRequiredError extends Error {
  constructor() {
    super("Your session ended. Sign in again to continue.");
    this.name = "AuthenticationRequiredError";
  }
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function requestHeaders(token: string, extra?: HeadersInit): Headers {
  const headers = new Headers(extra);
  headers.set("Authorization", `Bearer ${token}`);
  return headers;
}

async function authenticatedFetch(path: string, init?: RequestInit): Promise<Response> {
  const user = auth.currentUser;
  if (!user) throw new AuthenticationRequiredError();

  const send = async (forceRefresh: boolean) => {
    const token = await user.getIdToken(forceRefresh);
    return fetch(`${API_BASE}${path}`, {
      ...init,
      headers: requestHeaders(token, init?.headers),
    });
  };

  const response = await send(false);
  if (response.status !== 401) return response;

  // Firebase refreshes near-expiry tokens automatically. A single forced refresh
  // also recovers when the API rejects a token that expired between acquisition
  // and verification. Never loop and never retry under a different signed-in user.
  if (auth.currentUser !== user) throw new AuthenticationRequiredError();
  return send(true);
}

async function responseError(response: Response): Promise<ApiError> {
  const raw = await response.text();
  let detail = raw;
  if (raw && response.headers.get("Content-Type")?.includes("application/json")) {
    try {
      const value = JSON.parse(raw) as { detail?: unknown };
      if (typeof value.detail === "string") detail = value.detail;
    } catch {
      // Preserve the raw response when an upstream sends malformed JSON.
    }
  }
  return new ApiError(detail || `Request failed with status ${response.status}`, response.status);
}

async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await authenticatedFetch(path, init);
  if (!response.ok) {
    throw await responseError(response);
  }
  return (await response.json()) as T;
}

export function provisionAccount(): Promise<Account> {
  return apiRequest<Account>("/v1/accounts", { method: "POST" });
}

export function createBrowserCamera(name: string): Promise<BrowserCamera> {
  return apiRequest<BrowserCamera>("/v1/browser-cameras", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name }),
  });
}

export function listJournal(): Promise<MealEntry[]> {
  return apiRequest<MealEntry[]>("/v1/journal");
}

export function listOpenQuestions(): Promise<ClarificationQuestion[]> {
  return apiRequest<ClarificationQuestion[]>("/v1/questions");
}

export function listMealRevisions(mealId: string): Promise<MealRevision[]> {
  return apiRequest<MealRevision[]>(
    `/v1/meals/${encodeURIComponent(mealId)}/revisions`,
  );
}

export function submitMealFeedback(
  mealId: string,
  input: MealFeedbackInput,
  idempotencyKey: string,
): Promise<MealFeedbackResult> {
  return apiRequest<MealFeedbackResult>(
    `/v1/meals/${encodeURIComponent(mealId)}/feedback`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify(input),
    },
  );
}

export function answerQuestion(
  questionId: string,
  answer: string,
  learningTip: string | undefined,
  idempotencyKey: string,
): Promise<QuestionAnswerResult> {
  return apiRequest<QuestionAnswerResult>(
    `/v1/questions/${encodeURIComponent(questionId)}/answer`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify({ answer, learning_tip: learningTip }),
    },
  );
}

export function uploadCapture(
  cameraId: string,
  image: Blob,
  idempotencyKey: string,
  capture: BrowserCaptureMetadata,
): Promise<CaptureAccepted> {
  const form = new FormData();
  form.append("metadata", JSON.stringify({
    schema_version: 1,
    camera_id: cameraId,
    captured_at: capture.capturedAt,
    client_kind: "browser",
    client_version: "web-mvp/1",
    sequence_id: capture.sequenceId,
    sequence_number: capture.sequenceNumber,
    width: capture.width,
    height: capture.height,
  }));
  form.append("image", image, "capture.jpg");
  return apiRequest<CaptureAccepted>("/v1/captures", {
    method: "POST",
    headers: { "Idempotency-Key": idempotencyKey },
    body: form,
  });
}

export async function loadCaptureImage(captureId: string): Promise<string> {
  const response = await authenticatedFetch(
    `/v1/captures/${encodeURIComponent(captureId)}/image`,
  );
  if (!response.ok) {
    throw await responseError(response);
  }
  return URL.createObjectURL(await response.blob());
}
