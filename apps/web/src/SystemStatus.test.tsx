import { renderToStaticMarkup } from "react-dom/server";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it } from "vitest";
import type { Account, CaptureProcessing } from "./api";
import { SystemStatus, type PurchaseContextState } from "./SystemStatus";

const account: Account = {
  id: "account-1",
  owner_user_id: "user-1",
  entitlement_mode: "trial",
  trial_image_limit: 200,
  accepted_image_count: 12,
};

function renderStatus({
  currentAccount = account,
  processing = [],
  processingUnavailable = false,
  purchaseContext = "available",
  sessionStale = false,
}: {
  currentAccount?: Account;
  processing?: CaptureProcessing[];
  processingUnavailable?: boolean;
  purchaseContext?: PurchaseContextState;
  sessionStale?: boolean;
} = {}) {
  return renderToStaticMarkup(
    <MemoryRouter>
      <SystemStatus
        account={currentAccount}
        processing={processing}
        processingUnavailable={processingUnavailable}
        purchaseContext={purchaseContext}
        sessionStale={sessionStale}
      />
    </MemoryRouter>,
  );
}

function processing(stage: CaptureProcessing["stage"]): CaptureProcessing {
  return {
    capture_id: `capture-${stage}`,
    camera_id: "camera-1",
    captured_at: "2026-08-27T12:00:00Z",
    stage,
    attempt_count: stage.endsWith("retrying") ? 2 : 0,
    retry_at: stage.endsWith("retrying") ? "2026-08-27T12:05:00Z" : null,
    latest_failure_code: stage.endsWith("retrying") ? "provider_timeout" : null,
  };
}

describe("degraded system-state matrix", () => {
  it("keeps stale sessions, exhausted quota, retry, and missing purchases visible", () => {
    const html = renderStatus({
      currentAccount: { ...account, accepted_image_count: 200 },
      processing: [processing("analysis_retrying")],
      purchaseContext: "unavailable",
      sessionStale: true,
    });

    expect(html).toContain("API session ended");
    expect(html).toContain("quota is exhausted");
    expect(html).toContain("analysis failed and is scheduled to retry");
    expect(html).toContain("2 failed attempts");
    expect(html).toContain("provider_timeout");
    expect(html).toContain("Purchase context could not be loaded");
    expect(html).not.toContain("All recent images finished processing");
  });

  it("does not turn an unavailable processing read into an empty success", () => {
    const html = renderStatus({ processingUnavailable: true, purchaseContext: "empty" });

    expect(html).toContain("processing status is unavailable");
    expect(html).toContain("No supermarket purchase emails have been imported yet");
    expect(html).not.toContain("All recent images finished processing");
  });

  it.each([
    "storage_pending",
    "grouping_pending",
    "grouping_active",
    "grouping_retrying",
    "analysis_pending",
    "analysis_active",
    "analysis_retrying",
    "attention_required",
] as const)("renders %s as unresolved", (stage) => {
    const html = renderStatus({ processing: [processing(stage)] });

    expect(html).toContain("system-state--warning");
    expect(html).not.toContain("All recent images finished processing");
  });

  it("shows completion only after a completed backend record is loaded", () => {
    const html = renderStatus({ processing: [processing("complete")] });

    expect(html).toContain("All recent images finished processing");
  });
});
