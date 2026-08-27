import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  createContextNote: vi.fn(),
  listContextNotes: vi.fn(),
  retireContextNote: vi.fn(),
  teachKnowledge: vi.fn(),
}));
vi.mock("./auth", () => ({
  SessionControls: () => <div>Signed-in account controls</div>,
}));

import { ContextNoteCard } from "./ContextPage";
import type { UserContextNote } from "./api";

const activeNote: UserContextNote = {
  id: "note-1",
  account_id: "account-1",
  author_user_id: "owner-1",
  text: "My MIL brought duck, and we intend to cook it tomorrow.",
  valid_from: "2099-08-28T00:00:00Z",
  valid_until: "2099-08-29T00:00:00Z",
  status: "active",
  created_at: "2026-08-27T12:00:00Z",
  retired_at: null,
};

const handlers = {
  onEdit: vi.fn(),
  onRetire: vi.fn(),
  onPromote: vi.fn(),
};

describe("proactive context note", () => {
  it("shows scheduled exact wording, agent-use window, and lifecycle actions", () => {
    const html = renderToStaticMarkup(
      <ContextNoteCard note={activeNote} busy={false} {...handlers} />,
    );

    expect(html).toContain("scheduled");
    expect(html).toContain("My MIL brought duck");
    expect(html).toContain("Exact user wording");
    expect(html).toContain("Edit with history");
    expect(html).toContain("Make permanent knowledge");
    expect(html).toContain("Retire note");
  });

  it("keeps retired wording visible without active actions", () => {
    const html = renderToStaticMarkup(
      <ContextNoteCard
        note={{
          ...activeNote,
          status: "retired",
          retired_at: "2026-08-27T13:00:00Z",
        }}
        busy={false}
        {...handlers}
      />,
    );

    expect(html).toContain("retired");
    expect(html).toContain("My MIL brought duck");
    expect(html).not.toContain("Edit with history");
    expect(html).not.toContain("Make permanent knowledge");
    expect(html).not.toContain("Retire note");
  });
});
