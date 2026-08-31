import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { UnresolvedJournalCard } from "./App";
import type { JournalEvent } from "./api";

vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  classifyEvent: vi.fn(),
  loadCaptureImage: vi.fn(),
}));

function event(state: JournalEvent["state"]): JournalEvent {
  return {
    event_id: "event-unresolved-1",
    event_revision: 3,
    captured_at: "2026-08-31T10:26:39Z",
    camera_ids: ["camera-phone"],
    capture_ids: ["capture-1", "capture-2"],
    state,
    latest_failure_code: state === "error_processing" ? "InvalidModelOutputError" : null,
  };
}

function markup(state: JournalEvent["state"]): string {
  return renderToStaticMarkup(
    <UnresolvedJournalCard
      event={event(state)}
      onChanged={vi.fn()}
      onNotice={vi.fn()}
    />,
  );
}

describe("unresolved journal cards", () => {
  it("renders a processing burst as a normal recoverable journal card", () => {
    const html = markup("processing");

    expect(html).toContain("Processing");
    expect(html).toContain("Your photos are safely stored");
    expect(html).toContain("Frame 1 of 2");
    expect(html).toContain("Tell FoodLog what this was");
    expect(html).toContain("Save to journal");
    expect(html).toContain("Discard as not cooking");
  });

  it("keeps the same image and manual actions after processing errors", () => {
    const html = markup("error_processing");

    expect(html).toContain("Error processing");
    expect(html).toContain("Gemini did not produce a usable result");
    expect(html).toContain("Tell FoodLog what this was");
    expect(html).toContain("Discard as not cooking");
    expect(html).not.toContain("Looks right");
  });
});
