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

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8080";
const LOCAL_USER = "local-owner";

function requestHeaders(extra?: HeadersInit): Headers {
  const headers = new Headers(extra);
  headers.set("X-FoodLog-Local-User", LOCAL_USER);
  return headers;
}

async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: requestHeaders(init?.headers),
  });
  if (!response.ok) {
    const detail = await response.text();
    throw new Error(detail || `Request failed with status ${response.status}`);
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
): Promise<CaptureAccepted> {
  const form = new FormData();
  form.append("image", image, "capture.jpg");
  return apiRequest<CaptureAccepted>(
    `/v1/browser-cameras/${encodeURIComponent(cameraId)}/captures`,
    {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: form,
    },
  );
}

export async function loadCaptureImage(captureId: string): Promise<string> {
  const response = await fetch(
    `${API_BASE}/v1/captures/${encodeURIComponent(captureId)}/image`,
    { headers: requestHeaders() },
  );
  if (!response.ok) {
    throw new Error(`Image request failed with status ${response.status}`);
  }
  return URL.createObjectURL(await response.blob());
}
