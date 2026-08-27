import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import MealFeedbackControls, {
  CorrectionSummary,
  feedbackActionState,
} from "./MealFeedbackControls";
import type {
  ActivityInferenceKind,
  ActivityUserAction,
  MealEntry,
  MealStatus,
} from "./api";

function entryFor(
  kind: ActivityInferenceKind,
  status: MealStatus = "provisional",
  allowedActions: ActivityUserAction[] = [
    "confirm_guess",
    "correct",
    "discard_not_cooking",
  ],
): MealEntry {
  return {
    id: `meal-${kind}`,
    account_id: "account-1",
    capture_id: "capture-1",
    event_id: "event-1",
    occurred_at: "2026-08-27T12:00:00Z",
    title: kind === "unknown_activity" ? "Unknown kitchen activity" : "Likely steak",
    confidence: "uncertain",
    components: [{
      name: "Steak",
      ingredients: ["beef"],
      preparation_methods: ["air frying"],
    }],
    observations: ["Red meat is visible."],
    alternatives: ["Duck"],
    rationale: "The image is distant, so this remains tentative.",
    clarification_question: null,
    clarification_reason: null,
    activity_hypothesis: {
      schema_version: "activity-meal-inference-v1",
      event_id: "event-1",
      source_capture_ids: ["capture-1"],
      kind,
      best_guess: kind === "unknown_activity" ? null : "Steak",
      confidence: "uncertain",
      components: [],
      direct_observations: [],
      contextual_evidence: [],
      assumptions: [],
      deductions: [],
      alternatives: [],
      rationale: "The image is distant, so this remains tentative.",
      question: null,
      allowed_actions: allowedActions,
    },
    status,
    revision_number: 1,
    created_at: "2026-08-27T12:00:01Z",
  };
}

function controlsMarkup(entry: MealEntry): string {
  return renderToStaticMarkup(
    <MealFeedbackControls
      entry={entry}
      onChanged={vi.fn()}
      onNotice={vi.fn()}
    />,
  );
}

describe("state-correct meal feedback", () => {
  it("offers confirmation only for a named tentative meal that permits it", () => {
    const tentative = entryFor("tentative_meal");
    expect(feedbackActionState(tentative)).toEqual({
      canConfirm: true,
      canCorrect: true,
      canDiscard: true,
      correctionLabel: "Correct it",
    });

    const html = controlsMarkup(tentative);
    expect(html).toContain("Looks right");
    expect(html).toContain("Correct it");
    expect(html).toContain("Discard as not cooking");
  });

  it("never asks the user to approve a genuinely unknown activity", () => {
    const html = controlsMarkup(entryFor("unknown_activity"));

    expect(html).toContain("Tell FoodLog what this was");
    expect(html).toContain("Discard as not cooking");
    expect(html).not.toContain("Looks right");
    expect(html).not.toContain("What meal were you cooking");
  });

  it("does not offer confirmation for likely non-cooking activity", () => {
    const html = controlsMarkup(entryFor("likely_non_cooking"));

    expect(html).toContain("Correct classification");
    expect(html).toContain("Discard as not cooking");
    expect(html).not.toContain("Looks right");
  });

  it("keeps discarded activity recoverable without offering repeat discard", () => {
    const discarded = entryFor("likely_non_cooking", "not_cooking", ["correct"]);
    expect(feedbackActionState(discarded)).toEqual({
      canConfirm: false,
      canCorrect: true,
      canDiscard: false,
      correctionLabel: "Reclassify as cooking",
    });

    const html = controlsMarkup(discarded);
    expect(html).toContain("Reclassify as cooking");
    expect(html).not.toContain("Looks right");
    expect(html).not.toContain("Discard as not cooking");
  });

  it("renders the exact immutable correction target in revision history", () => {
    const componentHtml = renderToStaticMarkup(
      <CorrectionSummary correction={{
        scope: "component",
        component_index: 1,
        replacement: {
          name: "Duck breast",
          ingredients: ["duck"],
          preparation_methods: ["air frying"],
        },
      }} />,
    );
    const ingredientHtml = renderToStaticMarkup(
      <CorrectionSummary correction={{
        scope: "ingredient",
        component_index: 0,
        ingredient_index: 2,
        replacement: "oat cream",
      }} />,
    );

    expect(componentHtml).toContain("Component 2:");
    expect(componentHtml).toContain("Duck breast");
    expect(componentHtml).toContain("Ingredients: duck");
    expect(componentHtml).toContain("Preparation: air frying");
    expect(ingredientHtml).toContain("Ingredient 3 in component 1:");
    expect(ingredientHtml).toContain("oat cream");
  });
});
