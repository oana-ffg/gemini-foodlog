import { describe, expect, it } from "vitest";

import type { MealEntry } from "./api";
import { chronologicalJournal, mealOccurrence } from "./journal";

function entry(
  id: string,
  occurredAt: string | null,
  createdAt: string,
  status: MealEntry["status"],
): MealEntry {
  return {
    id,
    account_id: "account-1",
    capture_id: `capture-${id}`,
    event_id: `event-${id}`,
    occurred_at: occurredAt,
    activity_hypothesis: null,
    status,
    confidence: "likely",
    title: `Meal ${id}`,
    components: [],
    observations: ["Fixture observation"],
    alternatives: [],
    rationale: "Fixture rationale",
    clarification_question: null,
    clarification_reason: null,
    revision_number: 1,
    created_at: createdAt,
  };
}

describe("chronological journal", () => {
  it("orders by kitchen occurrence even when an older event was published later", () => {
    const olderPublishedLater = entry(
      "older",
      "2026-08-25T17:00:00Z",
      "2026-08-25T20:00:00Z",
      "provisional",
    );
    const newerPublishedFirst = entry(
      "newer",
      "2026-08-25T18:00:00Z",
      "2026-08-25T19:00:00Z",
      "confirmed",
    );

    expect(chronologicalJournal([olderPublishedLater, newerPublishedFirst]))
      .toEqual([newerPublishedFirst, olderPublishedLater]);
  });

  it("falls back to publication time for legacy entries without occurrence time", () => {
    const legacy = entry("legacy", null, "2026-08-25T16:00:00Z", "corrected");
    expect(mealOccurrence(legacy)).toBe("2026-08-25T16:00:00Z");
  });
});
