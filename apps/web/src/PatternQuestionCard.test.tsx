import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import PatternQuestionCard from "./PatternQuestionCard";
import type { ClarificationQuestion } from "./api";

const patternQuestion: ClarificationQuestion = {
  id: "pattern-question-1",
  account_id: "account-1",
  kind: "pattern_hypothesis",
  meal_id: null,
  event_id: null,
  prompt: "I am noticing you usually eat steak on Thursdays. Is that accurate?",
  reason: "Three dated meal revisions support this observation.",
  evidence: [
    { kind: "meal_revision", id: "meal-revision-1" },
    { kind: "meal_revision", id: "meal-revision-2" },
    { kind: "meal_revision", id: "meal-revision-counter" },
  ],
  choices: [],
  tentative_claim: "Thursday dinner is usually steak.",
  pattern_claim: {
    dimension: "likely meal",
    value: "steak",
    conditions: ["Thursday dinner"],
  },
  pattern_observation_started_at: "2026-08-01T00:00:00Z",
  pattern_observation_ended_at: "2026-08-22T00:00:00Z",
  pattern_supporting_examples: [
    {
      evidence: { kind: "meal_revision", id: "meal-revision-1" },
      occurred_at: "2026-08-06T18:00:00+02:00",
      occurred_utc_offset_minutes: 120,
      summary: "Steak was confirmed for Thursday dinner.",
    },
    {
      evidence: { kind: "meal_revision", id: "meal-revision-2" },
      occurred_at: "2026-08-13T18:00:00+02:00",
      occurred_utc_offset_minutes: 120,
      summary: "Another Thursday dinner was corrected to steak.",
    },
  ],
  pattern_counterexamples: [
    {
      evidence: { kind: "meal_revision", id: "meal-revision-counter" },
      occurred_at: "2026-08-20T18:00:00+02:00",
      occurred_utc_offset_minutes: 120,
      summary: "One Thursday dinner was chicken.",
    },
  ],
  pattern_prompt_version: "pattern-hypothesis-v1",
  pattern_uncertainty: "There is one counterexample in the observation window.",
  pattern_evidence_hash: "a".repeat(64),
  pattern_topic_key: "b".repeat(64),
  predecessor_question_id: "rejected-pattern-1",
  source_revision_number: null,
  status: "open",
  answer: null,
  learning_tip: null,
  response_kind: null,
  response_id: null,
  superseded_by_question_id: null,
  created_at: "2026-08-23T12:00:00Z",
  answered_at: null,
  superseded_at: null,
};

describe("pattern question card", () => {
  it("shows the exact claim, observation window, evidence, uncertainty, and response actions", () => {
    const html = renderToStaticMarkup(
      <PatternQuestionCard
        question={patternQuestion}
        onChanged={vi.fn()}
        onNotice={vi.fn()}
      />,
    );

    expect(html).toContain("Pattern FoodLog noticed");
    expect(html).toContain("Thursday dinner is usually steak");
    expect(html).toContain("Evidence from");
    expect(html).toContain("Supporting examples (2)");
    expect(html).toContain("Counterexamples (1)");
    expect(html).toContain("Steak was confirmed for Thursday dinner");
    expect(html).toContain("One Thursday dinner was chicken");
    expect(html).toContain("meal-revision-counter");
    expect(html).toContain("What may weaken this");
    expect(html).toContain("Revisited after rejected-pattern-1");
    expect(html).toContain("Yes, that is accurate");
    expect(html).toContain("Not quite — correct it");
    expect(html).toContain("No, this is not a pattern");
  });

  it("never renders a generic meal-identification form in the observations feed", () => {
    const html = renderToStaticMarkup(
      <PatternQuestionCard
        question={patternQuestion}
        onChanged={vi.fn()}
        onNotice={vi.fn()}
      />,
    );

    expect(html).not.toContain("What meal or ingredient was being prepared?");
    expect(html).not.toContain("What meal were you cooking?");
    expect(html).not.toContain("Answer and update journal");
    expect(html).not.toContain("Optional tip for next time");
  });
});
