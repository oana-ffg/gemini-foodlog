import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import {
  ActivityImageViewer,
  ActivityFocusedQuestion,
  ActivityRationale,
} from "./ActivityDetail";
import type { ActivityMealInference, MealInferenceSummary } from "./api";

vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  loadCaptureImage: vi.fn(),
}));

const summary: MealInferenceSummary = {
  title: "Likely steak with vegetables",
  confidence: "uncertain",
  components: [],
  observations: ["Red meat is visible by the sink."],
  alternatives: ["Duck"],
  rationale: "The red meat and air-fryer basket support the tentative guess.",
  clarification_question: null,
  clarification_reason: null,
};

const hypothesis: ActivityMealInference = {
  schema_version: "activity-meal-inference-v1",
  event_id: "event-1",
  source_capture_ids: ["capture-1", "capture-2"],
  kind: "tentative_meal",
  best_guess: "Steak with vegetables",
  confidence: "uncertain",
  direct_observations: [
    {
      id: "obs_meat",
      description: "A red piece of meat is being unwrapped.",
      image_evidence: [
        {
          capture_id: "capture-2",
          region: { x: 0.62, y: 0.45, width: 0.2, height: 0.18 },
        },
      ],
    },
  ],
  contextual_evidence: [
    {
      id: "context_purchase",
      description: "A recent purchase includes beef steak.",
      source_kind: "purchase",
      source_id: "purchase-1",
    },
  ],
  assumptions: [
    {
      id: "assumption_airfryer",
      description: "This household commonly air-fries steak.",
      knowledge_revision_id: "knowledge-revision-2",
    },
  ],
  deductions: [
    {
      id: "deduction_steak",
      description: "The visible meat is more likely beef than poultry.",
      evidence_ids: ["obs_meat", "context_purchase"],
    },
  ],
  components: [
    {
      id: "component_main",
      name: "Steak",
      ingredients: ["beef"],
      preparation_methods: ["air frying"],
      confidence: "likely",
      evidence_ids: ["deduction_steak"],
      alternatives: [
        {
          label: "Duck breast",
          reason: "The poor angle leaves the exact red meat ambiguous.",
          evidence_ids: ["obs_meat"],
        },
      ],
    },
  ],
  alternatives: [
    {
      label: "Duck with vegetables",
      reason: "Duck may also appear red from this distance.",
      evidence_ids: ["obs_meat"],
    },
  ],
  rationale: summary.rationale,
  question: {
    prompt: "Was this beef steak or duck breast?",
    justification: "The image cannot reliably distinguish the two red meats.",
    evidence_ids: ["obs_meat"],
    candidate_labels: ["Steak with vegetables", "Duck with vegetables"],
    impact: "changes_meal_identity",
  },
  allowed_actions: ["confirm_guess", "correct", "discard_not_cooking"],
};

describe("activity detail", () => {
  it("keeps every event frame reachable with explicit contain, zoom, pan, and reset UX", () => {
    const html = renderToStaticMarkup(
      <ActivityImageViewer
        captureIds={hypothesis.source_capture_ids}
        selectedCaptureId="capture-1"
        onSelectCapture={vi.fn()}
      />,
    );

    expect(html).toContain("Private event images");
    expect(html).toContain("Frame 1 of 2");
    expect(html).toContain("Previous frame");
    expect(html).toContain("Next frame");
    expect(html).toContain("Zoom out");
    expect(html).toContain("Zoom in");
    expect(html).toContain("Reset image");
    expect(html).toContain("full uncropped frame is contained at 100%");
    expect(html).toContain("drag to pan");
  });

  it("renders structured visual evidence, context, assumptions, deductions, and provenance", () => {
    const html = renderToStaticMarkup(
      <ActivityRationale
        inference={summary}
        hypothesis={hypothesis}
        onSelectCapture={vi.fn()}
      />,
    );

    expect(html).toContain("Direct visual observations");
    expect(html).toContain("A red piece of meat is being unwrapped");
    expect(html).toContain("Frame 2, x 62%, y 45%, width 20%, height 18%");
    expect(html).toContain("View this frame");
    expect(html).toContain("A recent purchase includes beef steak");
    expect(html).toContain("purchase-1");
    expect(html).toContain("This household commonly air-fries steak");
    expect(html).toContain("knowledge-revision-2");
    expect(html).toContain("The visible meat is more likely beef than poultry");
    expect(html).toContain("Ingredients: beef");
    expect(html).toContain("Preparation: air frying");
    expect(html).toContain("Duck with vegetables");
    expect(html).toContain("Was this beef steak or duck breast?");
    expect(html).toContain("activity-meal-inference-v1");
    expect(html).toContain("capture-1");
    expect(html).toContain("capture-2");
  });

  it("labels legacy inference detail honestly when structured provenance is absent", () => {
    const html = renderToStaticMarkup(
      <ActivityRationale inference={summary} hypothesis={null} />,
    );

    expect(html).toContain("Red meat is visible by the sink");
    expect(html).toContain("Duck");
    expect(html).toContain("predates structured evidence provenance");
    expect(html).not.toContain("Context used");
  });

  it("keeps a focused event question on its matching activity instead of the pattern feed", () => {
    const structuredHtml = renderToStaticMarkup(
      <ActivityFocusedQuestion inference={summary} hypothesis={hypothesis} />,
    );
    const legacyHtml = renderToStaticMarkup(
      <ActivityFocusedQuestion
        inference={{
          ...summary,
          clarification_question: "Was this steak or duck?",
          clarification_reason: "The distant angle leaves the meat colour ambiguous.",
        }}
        hypothesis={null}
      />,
    );

    expect(structuredHtml).toContain("Question about this event");
    expect(structuredHtml).toContain("Was this beef steak or duck breast?");
    expect(structuredHtml).toContain("obs_meat");
    expect(legacyHtml).toContain("Was this steak or duck?");
    expect(legacyHtml).toContain("distant angle");
  });
});
