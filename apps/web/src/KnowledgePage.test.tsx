import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  correctKnowledge: vi.fn(),
  getKnowledgePage: vi.fn(),
  listKnowledge: vi.fn(),
  retireKnowledge: vi.fn(),
  teachKnowledge: vi.fn(),
}));
vi.mock("./auth", () => ({
  SessionControls: () => <div>Signed-in account controls</div>,
}));

import { KnowledgeDetail } from "./KnowledgePage";
import type { KnowledgePageHistory } from "./api";

const history: KnowledgePageHistory = {
  page: {
    id: "page-1",
    account_id: "account-1",
    topic_key: "steak-by-sink",
    title: "Sink-side basket",
    statement: "The sink-side air-fryer basket usually means steak.",
    claim: {
      dimension: "meal",
      value: "steak",
      conditions: ["air-fryer basket by sink"],
    },
    lifecycle: "confirmed",
    belief_strength: "strong",
    current_revision_number: 2,
    current_revision_id: "revision-2",
    created_at: "2026-08-01T12:00:00Z",
    updated_at: "2026-08-27T12:00:00Z",
  },
  revisions: [
    {
      id: "revision-1",
      account_id: "account-1",
      page_id: "page-1",
      number: 1,
      title: "Sink-side basket",
      statement: "The basket means steak.",
      claim: null,
      lifecycle: "inferred",
      belief_strength: "moderate",
      source: "agent_inference",
      evidence: [{
        kind: "meal_revision",
        id: "meal-revision-1",
        role: "supports",
        note: "Observed on three Thursdays.",
      }],
      reason: "Repeated evidence suggested this pattern.",
      base_revision_number: null,
      previous_revision_id: null,
      created_at: "2026-08-01T12:00:00Z",
    },
    {
      id: "revision-2",
      account_id: "account-1",
      page_id: "page-1",
      number: 2,
      title: "Sink-side basket",
      statement: "The sink-side air-fryer basket usually means steak.",
      claim: null,
      lifecycle: "confirmed",
      belief_strength: "strong",
      source: "user_statement",
      evidence: [{
        kind: "knowledge_revision",
        id: "revision-1",
        role: "context",
        note: null,
      }],
      reason: "The user confirmed the household rule.",
      base_revision_number: 1,
      previous_revision_id: "revision-1",
      created_at: "2026-08-27T12:00:00Z",
    },
  ],
};

describe("household wiki detail", () => {
  it("shows current belief, correction and retirement actions, and exact provenance", () => {
    const html = renderToStaticMarkup(
      <KnowledgeDetail history={history} onChanged={vi.fn()} />,
    );

    expect(html).toContain("The sink-side air-fryer basket usually means steak.");
    expect(html).toContain("Correct this");
    expect(html).toContain("Retire belief");
    expect(html).toContain("Revision history (2)");
    expect(html).toContain("Observed on three Thursdays.");
    expect(html).toContain("meal-revision-1");
    expect(html).toContain("air-fryer basket by sink");
  });

  it("keeps retired knowledge inspectable but removes mutation actions", () => {
    const html = renderToStaticMarkup(
      <KnowledgeDetail
        history={{
          ...history,
          page: { ...history.page, lifecycle: "retired" },
        }}
        onChanged={vi.fn()}
      />,
    );

    expect(html).toContain("agent will not use it as current household knowledge");
    expect(html).not.toContain("Correct this");
    expect(html).not.toContain("Retire belief");
    expect(html).toContain("Revision history (2)");
  });
});
