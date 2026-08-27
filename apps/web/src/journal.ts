import type { MealEntry } from "./api";

function journalTimestamp(entry: MealEntry): number {
  return Date.parse(entry.occurred_at ?? entry.created_at);
}

export function chronologicalJournal(entries: readonly MealEntry[]): MealEntry[] {
  return [...entries].sort((left, right) => {
    const timeDifference = journalTimestamp(right) - journalTimestamp(left);
    return timeDifference || right.id.localeCompare(left.id);
  });
}

export function mealOccurrence(entry: MealEntry): string {
  return entry.occurred_at ?? entry.created_at;
}
