import { auth } from "./firebase";

export type Confidence = "confident" | "likely" | "uncertain";
export type MealStatus =
  | "provisional"
  | "confirmed"
  | "corrected"
  | "contradicted"
  | "not_cooking";
export type MealFeedbackKind = "confirm" | "correct" | "not_cooking";
export type MealRevisionSource = "inference" | "user_feedback";

export interface Account {
  id: string;
  owner_user_id: string;
  entitlement_mode: "trial" | "unlimited";
  trial_image_limit: number | null;
  accepted_image_count: number;
}

export interface ConsentPreferences {
  launch_mail_opt_in: boolean | null;
  launch_mail_policy_version: string | null;
  launch_mail_updated_at: string | null;
  waitlist_status: "not_joined" | "active" | "withdrawn";
  waitlist_policy_version: string | null;
  waitlist_updated_at: string | null;
}

export interface LaunchMailConsent {
  id: string;
  granted: boolean;
  policy_version: string;
  created_at: string;
}

export interface WaitlistEntry {
  id: string;
  email_normalized: string | null;
  policy_version: string;
  mailing_list_opt_in: boolean;
  status: "active" | "withdrawn";
  updated_at: string;
  last_withdrawn_at: string | null;
  withdrawal_count: number;
}

export interface BrowserCamera {
  id: string;
  account_id: string;
  name: string;
  kind: "browser";
  status: "active" | "revoked";
  accepted_capture_count: number;
  last_capture_at: string | null;
  created_at: string;
  revoked_at: string | null;
}

export interface DeviceCamera {
  id: string;
  account_id: string;
  name: string;
  kind: "device";
  status: "active" | "revoked";
  accepted_capture_count: number;
  last_capture_at: string | null;
  created_at: string;
  revoked_at: string | null;
}

export type Camera = BrowserCamera | DeviceCamera;

export interface DeviceCameraCredentialIssue {
  camera: DeviceCamera;
  credential: string;
}

export interface MealComponent {
  name: string;
  ingredients: string[];
  preparation_methods: string[];
}

export interface MealInferenceSummary {
  confidence: Confidence;
  title: string;
  components: MealComponent[];
  observations: string[];
  alternatives: string[];
  rationale: string;
  clarification_question: string | null;
  clarification_reason: string | null;
}

export type ActivityInferenceKind =
  | "tentative_meal"
  | "unknown_activity"
  | "likely_non_cooking";
export type ActivityUserAction =
  | "confirm_guess"
  | "correct"
  | "discard_not_cooking";
export type ContextSourceKind =
  | "purchase"
  | "household_knowledge"
  | "recent_meal"
  | "user_note";

export interface ImageRegion {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface ImageEvidenceLink {
  capture_id: string;
  region: ImageRegion | null;
}

export interface DirectObservation {
  id: string;
  description: string;
  image_evidence: ImageEvidenceLink[];
}

export interface ContextEvidence {
  id: string;
  description: string;
  source_kind: ContextSourceKind;
  source_id: string;
}

export interface ReasoningAssumption {
  id: string;
  description: string;
  knowledge_revision_id: string;
}

export interface Deduction {
  id: string;
  description: string;
  evidence_ids: string[];
}

export interface ActivityAlternative {
  label: string;
  reason: string;
  evidence_ids: string[];
}

export interface ActivityMealComponent {
  id: string;
  name: string;
  ingredients: string[];
  preparation_methods: string[];
  confidence: Confidence;
  alternatives: ActivityAlternative[];
  evidence_ids: string[];
}

export interface FocusedEventQuestion {
  prompt: string;
  justification: string;
  evidence_ids: string[];
  candidate_labels: string[];
  impact:
    | "changes_meal_identity"
    | "changes_food_trigger_relevance"
    | "changes_reusable_household_distinction";
}

export interface ActivityMealInference {
  schema_version: "activity-meal-inference-v1";
  event_id: string;
  source_capture_ids: string[];
  kind: ActivityInferenceKind;
  best_guess: string | null;
  confidence: Confidence;
  components: ActivityMealComponent[];
  direct_observations: DirectObservation[];
  contextual_evidence: ContextEvidence[];
  assumptions: ReasoningAssumption[];
  deductions: Deduction[];
  alternatives: ActivityAlternative[];
  rationale: string;
  question: FocusedEventQuestion | null;
  allowed_actions: ActivityUserAction[];
}

export interface MealEntry extends MealInferenceSummary {
  id: string;
  account_id: string;
  capture_id: string;
  event_id: string | null;
  occurred_at: string | null;
  activity_hypothesis: ActivityMealInference | null;
  status: MealStatus;
  revision_number: number;
  created_at: string;
}

export interface MealRevision {
  id: string;
  meal_id: string;
  number: number;
  status: MealStatus;
  source: MealRevisionSource;
  inference: MealInferenceSummary;
  activity_hypothesis: ActivityMealInference | null;
  created_at: string;
}

export interface MealFeedbackInput {
  kind: MealFeedbackKind;
  actual_meal?: string;
  explanation?: string;
  learning_disposition?: "reusable" | "insufficient_information";
}

export interface MealFeedbackResult {
  revision: MealRevision;
  learning_outcome:
    | "confirmation_only"
    | "not_cooking"
    | "wrong_only"
    | "meal_only"
    | "insufficient_information"
    | "unclassified_explanation"
    | "knowledge_applied";
  knowledge: {
    page: { id: string; title: string; statement: string };
    revision: { id: string; number: number; statement: string };
  } | null;
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

export type KnowledgeLifecycle =
  | "inferred"
  | "reinforced"
  | "confirmed"
  | "contradicted"
  | "retired";
export type KnowledgeBeliefStrength = "weak" | "moderate" | "strong";
export type KnowledgeRevisionSource =
  | "agent_inference"
  | "user_feedback"
  | "user_statement"
  | "question_response";
export type KnowledgeEvidenceKind =
  | "capture"
  | "meal_revision"
  | "feedback"
  | "question_response"
  | "purchase_document"
  | "user_context_note"
  | "knowledge_revision";
export type KnowledgeEvidenceRole = "supports" | "contradicts" | "context";

export interface KnowledgeClaim {
  dimension: string;
  value: string;
  conditions: string[];
}

export interface KnowledgeEvidenceReference {
  kind: KnowledgeEvidenceKind;
  id: string;
  role: KnowledgeEvidenceRole;
  note: string | null;
}

export interface KnowledgePage {
  id: string;
  account_id: string;
  topic_key: string;
  title: string;
  statement: string;
  claim: KnowledgeClaim | null;
  lifecycle: KnowledgeLifecycle;
  belief_strength: KnowledgeBeliefStrength;
  current_revision_number: number;
  current_revision_id: string;
  created_at: string;
  updated_at: string;
}

export interface KnowledgeRevision {
  id: string;
  account_id: string;
  page_id: string;
  number: number;
  title: string;
  statement: string;
  claim: KnowledgeClaim | null;
  lifecycle: KnowledgeLifecycle;
  belief_strength: KnowledgeBeliefStrength;
  source: KnowledgeRevisionSource;
  evidence: KnowledgeEvidenceReference[];
  reason: string;
  base_revision_number: number | null;
  previous_revision_id: string | null;
  created_at: string;
}

export interface KnowledgePageHistory {
  page: KnowledgePage;
  revisions: KnowledgeRevision[];
}

export interface UserContextNote {
  id: string;
  account_id: string;
  author_user_id: string;
  text: string;
  valid_from: string | null;
  valid_until: string | null;
  status: "active" | "retired";
  created_at: string;
  retired_at: string | null;
}

export interface KnowledgeRevisionResult {
  page: KnowledgePage;
  revision: KnowledgeRevision;
}

export interface StableKnowledgeTeachingResult extends KnowledgeRevisionResult {
  source_note: UserContextNote;
}

export interface UserContextNoteInput {
  text: string;
  valid_from?: string;
  valid_until?: string;
}

export interface CaptureAccepted {
  capture_id: string;
  accepted_image_count: number;
  entitlement_mode: "trial" | "unlimited";
  trial_image_limit: number | null;
  duplicate: boolean;
}

export type ProcessingStage =
  | "storage_pending"
  | "grouping_pending"
  | "grouping_active"
  | "grouping_retrying"
  | "analysis_pending"
  | "analysis_active"
  | "analysis_retrying"
  | "complete"
  | "attention_required";

export interface CaptureProcessing {
  capture_id: string;
  camera_id: string;
  captured_at: string;
  stage: ProcessingStage;
  attempt_count: number;
  retry_at: string | null;
  latest_failure_code: string | null;
}

export interface PurchaseSummary {
  id: string;
  merchant: string;
  revision_count: number;
  latest_confirmation_document_id: string | null;
  latest_final_document_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface BrowserCaptureMetadata {
  capturedAt: string;
  sequenceId: string;
  sequenceNumber: number;
  width: number;
  height: number;
  burstId?: string;
  burstFrameIndex?: number;
  motion?: {
    detected: boolean;
    algorithm: string;
    score: number;
    changedPixelRatio: number;
    threshold: number;
  };
}

export const API_BASE_URL = import.meta.env.VITE_API_BASE ?? (
  import.meta.env.PROD
    ? "https://foodlog-api-sptvo5nsga-ew.a.run.app"
    : "http://127.0.0.1:8080"
);

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
    return fetch(`${API_BASE_URL}${path}`, {
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
  const refreshedResponse = await send(true);
  if (refreshedResponse.status === 401) throw new AuthenticationRequiredError();
  return refreshedResponse;
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

export function listProcessing(limit = 20): Promise<CaptureProcessing[]> {
  return apiRequest<CaptureProcessing[]>(`/v1/processing?limit=${limit}`);
}

export function listPurchases(limit = 20): Promise<PurchaseSummary[]> {
  return apiRequest<PurchaseSummary[]>(`/v1/purchases?limit=${limit}`);
}

export function getConsentPreferences(): Promise<ConsentPreferences> {
  return apiRequest<ConsentPreferences>("/v1/consents");
}

export function recordLaunchMailConsent(granted: boolean): Promise<LaunchMailConsent> {
  return apiRequest<LaunchMailConsent>("/v1/consents/launch-mail", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ granted }),
  });
}

export function withdrawLaunchMailConsent(): Promise<LaunchMailConsent> {
  return apiRequest<LaunchMailConsent>("/v1/consents/launch-mail/withdraw", {
    method: "POST",
  });
}

export function joinWaitlist(): Promise<WaitlistEntry> {
  return apiRequest<WaitlistEntry>("/v1/waitlist", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ join: true }),
  });
}

export function withdrawWaitlist(): Promise<WaitlistEntry> {
  return apiRequest<WaitlistEntry>("/v1/waitlist/withdraw", {
    method: "POST",
  });
}

export function createBrowserCamera(
  name: string,
  clientInstanceId: string,
): Promise<BrowserCamera> {
  return apiRequest<BrowserCamera>("/v1/browser-cameras", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, client_instance_id: clientInstanceId }),
  });
}

export function listCameras(): Promise<Camera[]> {
  return apiRequest<Camera[]>("/v1/cameras");
}

export function revokeCamera(cameraId: string): Promise<Camera> {
  return apiRequest<Camera>(
    `/v1/cameras/${encodeURIComponent(cameraId)}/revoke`,
    { method: "POST" },
  );
}

export function createDeviceCamera(name: string): Promise<DeviceCameraCredentialIssue> {
  return apiRequest<DeviceCameraCredentialIssue>("/v1/device-cameras", {
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

export function listKnowledge(includeRetired = false): Promise<KnowledgePage[]> {
  const query = includeRetired ? "?include_retired=true" : "";
  return apiRequest<KnowledgePage[]>(`/v1/knowledge${query}`);
}

export function getKnowledgePage(pageId: string): Promise<KnowledgePageHistory> {
  return apiRequest<KnowledgePageHistory>(
    `/v1/knowledge/${encodeURIComponent(pageId)}`,
  );
}

export function teachKnowledge(
  statement: string,
  idempotencyKey: string,
): Promise<StableKnowledgeTeachingResult> {
  return apiRequest<StableKnowledgeTeachingResult>("/v1/knowledge", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify({ statement }),
  });
}

export function correctKnowledge(
  pageId: string,
  statement: string,
  expectedRevisionNumber: number,
  idempotencyKey: string,
): Promise<StableKnowledgeTeachingResult> {
  return apiRequest<StableKnowledgeTeachingResult>(
    `/v1/knowledge/${encodeURIComponent(pageId)}/correct`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify({
        statement,
        expected_revision_number: expectedRevisionNumber,
      }),
    },
  );
}

export function retireKnowledge(
  pageId: string,
  expectedRevisionNumber: number,
  reason: string | undefined,
  idempotencyKey: string,
): Promise<KnowledgeRevisionResult> {
  return apiRequest<KnowledgeRevisionResult>(
    `/v1/knowledge/${encodeURIComponent(pageId)}/retire`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify({
        expected_revision_number: expectedRevisionNumber,
        reason,
      }),
    },
  );
}

export function createContextNote(
  input: UserContextNoteInput,
  idempotencyKey: string,
): Promise<UserContextNote> {
  return apiRequest<UserContextNote>("/v1/context-notes", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify(input),
  });
}

export function listContextNotes(includeInactive = false): Promise<UserContextNote[]> {
  const query = includeInactive ? "?include_inactive=true" : "";
  return apiRequest<UserContextNote[]>(`/v1/context-notes${query}`);
}

export function retireContextNote(noteId: string): Promise<UserContextNote> {
  return apiRequest<UserContextNote>(
    `/v1/context-notes/${encodeURIComponent(noteId)}/retire`,
    { method: "POST" },
  );
}

export function uploadCapture(
  cameraId: string,
  image: Blob,
  idempotencyKey: string,
  capture: BrowserCaptureMetadata,
): Promise<CaptureAccepted> {
  if ((capture.burstId === undefined) !== (capture.burstFrameIndex === undefined)) {
    throw new Error("Motion burst ID and frame index must be supplied together.");
  }
  const form = new FormData();
  const metadata: Record<string, unknown> = {
    schema_version: 1,
    camera_id: cameraId,
    captured_at: capture.capturedAt,
    client_kind: "browser",
    client_version: "web-mvp/1",
    sequence_id: capture.sequenceId,
    sequence_number: capture.sequenceNumber,
    width: capture.width,
    height: capture.height,
  };
  if (capture.burstId !== undefined && capture.burstFrameIndex !== undefined) {
    metadata.burst_id = capture.burstId;
    metadata.burst_frame_index = capture.burstFrameIndex;
  }
  if (capture.motion !== undefined) {
    metadata.motion = {
      detected: capture.motion.detected,
      algorithm: capture.motion.algorithm,
      score: capture.motion.score,
      changed_pixel_ratio: capture.motion.changedPixelRatio,
      threshold: capture.motion.threshold,
    };
  }
  form.append("metadata", JSON.stringify(metadata));
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
