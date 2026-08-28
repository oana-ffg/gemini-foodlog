import { Link } from "react-router-dom";
import type { Account, CaptureProcessing } from "./api";

export type PurchaseContextState = "loading" | "available" | "empty" | "unavailable";

interface SystemStatusProps {
  account?: Account;
  processing?: CaptureProcessing[];
  processingUnavailable: boolean;
  purchaseContext: PurchaseContextState;
  sessionStale: boolean;
}

const stageText: Record<CaptureProcessing["stage"], string> = {
  storage_pending: "Upload accepted; secure storage is not confirmed yet.",
  grouping_pending: "Stored image is waiting to be grouped into a kitchen event.",
  grouping_active: "Stored image is being grouped into a kitchen event.",
  grouping_retrying: "Event grouping failed and is scheduled to retry.",
  analysis_pending: "Kitchen event is waiting for Gemini analysis.",
  analysis_active: "Gemini analysis is running.",
  analysis_retrying: "Gemini analysis failed and is scheduled to retry.",
  evaluation_complete: "An internal model evaluation finished without publishing a journal result.",
  complete: "Analysis completed.",
  attention_required: "Processing did not reach a valid next stage and needs attention.",
};

function ProcessingItem({ item }: { item: CaptureProcessing }) {
  const unresolved = item.stage !== "complete";
  return (
    <li className={unresolved ? "system-state system-state--warning" : "system-state"}>
      <div>
        <strong>{stageText[item.stage]}</strong>
        <small>
          Captured {new Date(item.captured_at).toLocaleString()}
          {item.attempt_count > 0 ? ` · ${item.attempt_count} failed attempt${item.attempt_count === 1 ? "" : "s"}` : ""}
        </small>
      </div>
      {item.retry_at ? <time dateTime={item.retry_at}>Retry {new Date(item.retry_at).toLocaleString()}</time> : null}
      {item.latest_failure_code ? <code>{item.latest_failure_code}</code> : null}
    </li>
  );
}

export function SystemStatus({
  account,
  processing,
  processingUnavailable,
  purchaseContext,
  sessionStale,
}: SystemStatusProps) {
  const unresolved = processing?.filter((item) => item.stage !== "complete") ?? [];
  const quotaExhausted = account?.entitlement_mode === "trial"
    && account.trial_image_limit !== null
    && account.accepted_image_count >= account.trial_image_limit;

  return (
    <section className="system-status" aria-labelledby="system-status-title">
      <div className="section-heading">
        <div>
          <p className="section-kicker">Live system state</p>
          <h2 id="system-status-title">What FoodLog is doing</h2>
        </div>
        <Link to="/camera">Open capture controls</Link>
      </div>

      <ul className="system-state-list">
        {sessionStale ? (
          <li className="system-state system-state--error" role="alert">
            <strong>Your API session ended. Sign in again before trusting this dashboard.</strong>
          </li>
        ) : null}
        {quotaExhausted ? (
          <li className="system-state system-state--error" role="alert">
            <strong>The image quota is exhausted. New captures are blocked.</strong>
          </li>
        ) : account ? (
          <li className="system-state">
            <strong>
              {account.entitlement_mode === "unlimited"
                ? "Image capture is not quota-limited."
                : `${account.trial_image_limit! - account.accepted_image_count} trial images remain.`}
            </strong>
          </li>
        ) : (
          <li className="system-state"><strong>Checking image entitlement…</strong></li>
        )}

        {processingUnavailable ? (
          <li className="system-state system-state--error" role="alert">
            <strong>Backend processing status is unavailable. Recent images may still be pending or failed.</strong>
          </li>
        ) : processing === undefined ? (
          <li className="system-state"><strong>Checking recent image processing…</strong></li>
        ) : unresolved.length > 0 ? (
          unresolved.map((item) => <ProcessingItem key={item.capture_id} item={item} />)
        ) : (
          <li className="system-state">
            <strong>
              {processing.length === 0
                ? "No images have entered the processing pipeline yet."
                : "All recent images finished processing."}
            </strong>
          </li>
        )}

        {purchaseContext === "unavailable" ? (
          <li className="system-state system-state--warning" role="alert">
            <strong>Purchase context could not be loaded. Image analysis may have less evidence.</strong>
          </li>
        ) : purchaseContext === "empty" ? (
          <li className="system-state">
            <strong>No supermarket purchase emails have been imported yet.</strong>
          </li>
        ) : purchaseContext === "available" ? (
          <li className="system-state"><strong>Purchase context is available to the agent.</strong></li>
        ) : (
          <li className="system-state"><strong>Checking purchase context…</strong></li>
        )}
      </ul>
    </section>
  );
}
