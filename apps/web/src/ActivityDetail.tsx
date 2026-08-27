import {
  useEffect,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
} from "react";
import {
  loadCaptureImage,
  type ActivityMealInference,
  type ImageRegion,
  type MealInferenceSummary,
} from "./api";

const MIN_ZOOM = 1;
const MAX_ZOOM = 4;
const ZOOM_STEP = 0.5;

interface Point {
  x: number;
  y: number;
}

interface DragStart extends Point {
  pointerId: number;
  origin: Point;
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(maximum, Math.max(minimum, value));
}

function clampPan(
  viewport: HTMLDivElement | null,
  zoom: number,
  point: Point,
): Point {
  if (!viewport || zoom === MIN_ZOOM) return { x: 0, y: 0 };
  const maximumX = (viewport.clientWidth * (zoom - 1)) / 2;
  const maximumY = (viewport.clientHeight * (zoom - 1)) / 2;
  return {
    x: clamp(point.x, -maximumX, maximumX),
    y: clamp(point.y, -maximumY, maximumY),
  };
}

function humanize(value: string): string {
  return value.replaceAll("_", " ");
}

function regionLabel(region: ImageRegion | null): string {
  if (!region) return "whole frame";
  const percent = (value: number) => `${Math.round(value * 100)}%`;
  return [
    `x ${percent(region.x)}`,
    `y ${percent(region.y)}`,
    `width ${percent(region.width)}`,
    `height ${percent(region.height)}`,
  ].join(", ");
}

interface ActivityImageViewerProps {
  captureIds: string[];
  selectedCaptureId: string;
  onSelectCapture: (captureId: string) => void;
}

export function ActivityImageViewer({
  captureIds,
  selectedCaptureId,
  onSelectCapture,
}: ActivityImageViewerProps) {
  const [imageUrl, setImageUrl] = useState<string>();
  const [message, setMessage] = useState("Loading private image…");
  const [zoom, setZoom] = useState(MIN_ZOOM);
  const [pan, setPan] = useState<Point>({ x: 0, y: 0 });
  const viewportRef = useRef<HTMLDivElement>(null);
  const dragStart = useRef<DragStart | undefined>(undefined);
  const selectedIndex = Math.max(0, captureIds.indexOf(selectedCaptureId));

  useEffect(() => {
    let active = true;
    let objectUrl: string | undefined;
    setImageUrl(undefined);
    setMessage("Loading private image…");
    setZoom(MIN_ZOOM);
    setPan({ x: 0, y: 0 });
    dragStart.current = undefined;

    loadCaptureImage(selectedCaptureId)
      .then((url) => {
        objectUrl = url;
        if (!active) {
          URL.revokeObjectURL(url);
          return;
        }
        setImageUrl(url);
        setMessage("");
      })
      .catch((error: unknown) => {
        if (!active) return;
        setMessage(
          error instanceof Error
            ? error.message
            : "The private image could not be loaded.",
        );
      });

    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [selectedCaptureId]);

  const selectIndex = (index: number) => {
    const nextId = captureIds[index];
    if (nextId) onSelectCapture(nextId);
  };

  const changeZoom = (delta: number) => {
    const nextZoom = clamp(zoom + delta, MIN_ZOOM, MAX_ZOOM);
    setZoom(nextZoom);
    setPan((current) => clampPan(viewportRef.current, nextZoom, current));
  };

  const resetView = () => {
    setZoom(MIN_ZOOM);
    setPan({ x: 0, y: 0 });
  };

  const startPan = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (zoom === MIN_ZOOM) return;
    event.currentTarget.setPointerCapture(event.pointerId);
    dragStart.current = {
      pointerId: event.pointerId,
      x: event.clientX,
      y: event.clientY,
      origin: pan,
    };
  };

  const movePan = (event: ReactPointerEvent<HTMLDivElement>) => {
    const start = dragStart.current;
    if (!start || start.pointerId !== event.pointerId) return;
    setPan(clampPan(event.currentTarget, zoom, {
      x: start.origin.x + event.clientX - start.x,
      y: start.origin.y + event.clientY - start.y,
    }));
  };

  const stopPan = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (dragStart.current?.pointerId !== event.pointerId) return;
    if (event.currentTarget.hasPointerCapture(event.pointerId)) {
      event.currentTarget.releasePointerCapture(event.pointerId);
    }
    dragStart.current = undefined;
  };

  return (
    <section className="evidence-viewer" aria-label="Private event images">
      <div
        ref={viewportRef}
        className={`evidence-viewer__viewport${zoom > 1 ? " is-zoomed" : ""}`}
        onPointerDown={startPan}
        onPointerMove={movePan}
        onPointerUp={stopPan}
        onPointerCancel={stopPan}
      >
        {imageUrl ? (
          <img
            src={imageUrl}
            alt={`Captured kitchen evidence, frame ${selectedIndex + 1} of ${captureIds.length}`}
            draggable={false}
            style={{
              transform: `translate3d(${pan.x}px, ${pan.y}px, 0) scale(${zoom})`,
            }}
          />
        ) : (
          <p role="status">{message}</p>
        )}
      </div>
      <div className="evidence-viewer__controls" aria-label="Image controls">
        {captureIds.length > 1 ? (
          <>
            <button
              type="button"
              className="button--quiet"
              onClick={() => selectIndex(selectedIndex - 1)}
              disabled={selectedIndex === 0}
            >
              Previous frame
            </button>
            <span>Frame {selectedIndex + 1} of {captureIds.length}</span>
            <button
              type="button"
              className="button--quiet"
              onClick={() => selectIndex(selectedIndex + 1)}
              disabled={selectedIndex === captureIds.length - 1}
            >
              Next frame
            </button>
          </>
        ) : (
          <span>Frame 1 of 1</span>
        )}
        <button
          type="button"
          className="button--quiet"
          onClick={() => changeZoom(-ZOOM_STEP)}
          disabled={zoom === MIN_ZOOM}
          aria-label="Zoom out"
        >
          −
        </button>
        <span aria-live="polite">{Math.round(zoom * 100)}%</span>
        <button
          type="button"
          className="button--quiet"
          onClick={() => changeZoom(ZOOM_STEP)}
          disabled={zoom === MAX_ZOOM}
          aria-label="Zoom in"
        >
          +
        </button>
        <button
          type="button"
          className="button--quiet"
          onClick={resetView}
          disabled={zoom === MIN_ZOOM && pan.x === 0 && pan.y === 0}
        >
          Reset image
        </button>
      </div>
      <p className="evidence-viewer__help">
        The full uncropped frame is contained at 100%. Zoom in, then drag to pan.
      </p>
    </section>
  );
}

interface ActivityRationaleProps {
  inference: MealInferenceSummary;
  hypothesis: ActivityMealInference | null;
  onSelectCapture?: (captureId: string) => void;
  includeQuestion?: boolean;
}

function EvidenceIds({ ids }: { ids: string[] }) {
  return ids.length > 0 ? <small>Evidence: {ids.join(", ")}</small> : null;
}

export function ActivityFocusedQuestion({
  inference,
  hypothesis,
}: Pick<ActivityRationaleProps, "inference" | "hypothesis">) {
  const prompt = hypothesis?.question?.prompt ?? inference.clarification_question;
  if (!prompt) return null;
  const reason = hypothesis?.question?.justification ?? inference.clarification_reason;
  return (
    <aside className="event-question" aria-label="Focused question about this event">
      <p className="section-kicker">Question about this event</p>
      <h4>{prompt}</h4>
      {reason ? <p>{reason}</p> : null}
      {hypothesis?.question ? (
        <EvidenceIds ids={hypothesis.question.evidence_ids} />
      ) : null}
      <small>Use the activity actions below to confirm the guess or give the exact correction.</small>
    </aside>
  );
}

export function ActivityRationale({
  inference,
  hypothesis,
  onSelectCapture,
  includeQuestion = true,
}: ActivityRationaleProps) {
  if (!hypothesis) {
    return (
      <details className="activity-rationale">
        <summary>Evidence, components, and alternatives</summary>
        <h4>Observed</h4>
        <ul>{inference.observations.map((item) => <li key={item}>{item}</li>)}</ul>
        {inference.components.length > 0 ? (
          <>
            <h4>Meal components</h4>
            <ul>
              {inference.components.map((component) => (
                <li key={component.name}>
                  <strong>{component.name}</strong>
                  {component.ingredients.length > 0
                    ? ` — ingredients: ${component.ingredients.join(", ")}`
                    : ""}
                  {component.preparation_methods.length > 0
                    ? ` — preparation: ${component.preparation_methods.join(", ")}`
                    : ""}
                </li>
              ))}
            </ul>
          </>
        ) : null}
        {inference.alternatives.length > 0 ? (
          <>
            <h4>Alternatives</h4>
            <ul>{inference.alternatives.map((item) => <li key={item}>{item}</li>)}</ul>
          </>
        ) : null}
        <p className="legacy-evidence-note">
          This older entry predates structured evidence provenance.
        </p>
      </details>
    );
  }

  const captureIndex = new Map(
    hypothesis.source_capture_ids.map((captureId, index) => [captureId, index + 1]),
  );

  return (
    <details className="activity-rationale">
      <summary>Evidence, context, assumptions, and alternatives</summary>
      <section>
        <h4>Direct visual observations</h4>
        <ol className="evidence-list">
          {hypothesis.direct_observations.map((observation) => (
            <li key={observation.id}>
              <strong>{observation.description}</strong>
              <small>ID: {observation.id}</small>
              <ul>
                {observation.image_evidence.map((link) => (
                  <li key={`${observation.id}-${link.capture_id}`}>
                    Frame {captureIndex.get(link.capture_id) ?? "?"}, {regionLabel(link.region)}
                    {onSelectCapture ? (
                      <button
                        type="button"
                        className="text-button evidence-link"
                        onClick={() => onSelectCapture(link.capture_id)}
                      >
                        View this frame
                      </button>
                    ) : null}
                  </li>
                ))}
              </ul>
            </li>
          ))}
        </ol>
      </section>

      <section>
        <h4>Context used</h4>
        {hypothesis.contextual_evidence.length > 0 ? (
          <ul className="evidence-list">
            {hypothesis.contextual_evidence.map((context) => (
              <li key={context.id}>
                <strong>{context.description}</strong>
                <small>
                  {humanize(context.source_kind)}: {context.source_id} · ID: {context.id}
                </small>
              </li>
            ))}
          </ul>
        ) : <p>No household or purchase context was used.</p>}
      </section>

      <section>
        <h4>Assumptions</h4>
        {hypothesis.assumptions.length > 0 ? (
          <ul className="evidence-list">
            {hypothesis.assumptions.map((assumption) => (
              <li key={assumption.id}>
                <strong>{assumption.description}</strong>
                <small>
                  Household knowledge revision: {assumption.knowledge_revision_id} · ID: {assumption.id}
                </small>
              </li>
            ))}
          </ul>
        ) : <p>No household assumption was applied.</p>}
      </section>

      <section>
        <h4>Deductions</h4>
        <ul className="evidence-list">
          {hypothesis.deductions.map((deduction) => (
            <li key={deduction.id}>
              <strong>{deduction.description}</strong>
              <EvidenceIds ids={deduction.evidence_ids} />
            </li>
          ))}
        </ul>
      </section>

      {hypothesis.components.length > 0 ? (
        <section>
          <h4>Meal components</h4>
          <ul className="evidence-list">
            {hypothesis.components.map((component) => (
              <li key={component.id}>
                <strong>{component.name}</strong> · {component.confidence}
                {component.ingredients.length > 0
                  ? <span>Ingredients: {component.ingredients.join(", ")}</span>
                  : null}
                {component.preparation_methods.length > 0
                  ? <span>Preparation: {component.preparation_methods.join(", ")}</span>
                  : null}
                <EvidenceIds ids={component.evidence_ids} />
                {component.alternatives.length > 0 ? (
                  <ul>
                    {component.alternatives.map((alternative) => (
                      <li key={alternative.label}>
                        <strong>Alternative: {alternative.label}</strong> — {alternative.reason}
                        <EvidenceIds ids={alternative.evidence_ids} />
                      </li>
                    ))}
                  </ul>
                ) : null}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <section>
        <h4>Whole-event alternatives</h4>
        {hypothesis.alternatives.length > 0 ? (
          <ul className="evidence-list">
            {hypothesis.alternatives.map((alternative) => (
              <li key={alternative.label}>
                <strong>{alternative.label}</strong> — {alternative.reason}
                <EvidenceIds ids={alternative.evidence_ids} />
              </li>
            ))}
          </ul>
        ) : <p>No named alternative was supported.</p>}
      </section>

      {includeQuestion && hypothesis.question ? (
        <section>
          <h4>Focused question</h4>
          <p>{hypothesis.question.prompt}</p>
          <p>{hypothesis.question.justification}</p>
          <EvidenceIds ids={hypothesis.question.evidence_ids} />
        </section>
      ) : null}

      <section className="activity-provenance">
        <h4>Provenance</h4>
        <p>Event: {hypothesis.event_id}</p>
        <p>Inference contract: {hypothesis.schema_version}</p>
        <ol>
          {hypothesis.source_capture_ids.map((captureId) => (
            <li key={captureId}>Frame {captureIndex.get(captureId)}: {captureId}</li>
          ))}
        </ol>
      </section>
    </details>
  );
}
