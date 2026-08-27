import { useState } from "react";
import {
  respondToPatternQuestion,
  type ClarificationQuestion,
  type PatternEvidenceExample,
  type QuestionResponseKind,
  type QuestionResponseResult,
} from "./api";

function formatObservationDate(value: string): string {
  return new Date(value).toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function formatOffset(minutes: number | null): string | null {
  if (minutes === null) return null;
  const sign = minutes >= 0 ? "+" : "−";
  const absoluteMinutes = Math.abs(minutes);
  const hours = Math.floor(absoluteMinutes / 60);
  const remainder = absoluteMinutes % 60;
  return `UTC${sign}${hours}${remainder ? `:${String(remainder).padStart(2, "0")}` : ""}`;
}

function EvidenceExamples({
  examples,
  emptyLabel,
}: {
  examples: PatternEvidenceExample[];
  emptyLabel: string;
}) {
  if (examples.length === 0) {
    return <p className="pattern-evidence__empty">{emptyLabel}</p>;
  }
  return (
    <ol className="pattern-evidence">
      {examples.map((example) => {
        const offset = formatOffset(example.occurred_utc_offset_minutes);
        return (
          <li key={`${example.evidence.kind}-${example.evidence.id}`}>
            <time dateTime={example.occurred_at}>
              {formatObservationDate(example.occurred_at)}{offset ? ` (${offset})` : ""}
            </time>
            <p>{example.summary}</p>
            <small>{example.evidence.kind.replaceAll("_", " ")} · {example.evidence.id}</small>
          </li>
        );
      })}
    </ol>
  );
}

function responseNotice(
  kind: QuestionResponseKind,
  result: QuestionResponseResult,
): string {
  if (kind === "reject") {
    return "Pattern rejected. It will need materially new evidence before FoodLog can surface it again.";
  }
  const statement = result.knowledge?.revision.statement;
  if (kind === "correct") {
    return statement
      ? `Correction saved exactly as written: “${statement}”`
      : "Correction saved exactly as written.";
  }
  return statement
    ? `Pattern confirmed and saved as household knowledge: “${statement}”`
    : "Pattern confirmed.";
}

interface PatternQuestionCardProps {
  question: ClarificationQuestion;
  onChanged: () => Promise<void>;
  onNotice: (message: string) => void;
}

export default function PatternQuestionCard({
  question,
  onChanged,
  onNotice,
}: PatternQuestionCardProps) {
  const [mode, setMode] = useState<"correct" | "reject">();
  const [correction, setCorrection] = useState("");
  const [explanation, setExplanation] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string>();

  const respond = async (kind: QuestionResponseKind) => {
    const correctedWording = correction.trim();
    if (kind === "correct" && !correctedWording) {
      setMessage("Write the corrected pattern in your own words.");
      return;
    }
    setBusy(true);
    setMessage("Saving your response…");
    let result: QuestionResponseResult;
    try {
      result = await respondToPatternQuestion(
        question.id,
        {
          kind,
          correction: kind === "correct" ? correctedWording : undefined,
          explanation: explanation.trim() || undefined,
        },
        crypto.randomUUID(),
      );
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Could not save the response");
      setBusy(false);
      return;
    }
    const notice = responseNotice(kind, result);
    setMessage(notice);
    setBusy(false);
    onNotice(notice);
    try {
      await onChanged();
    } catch (error: unknown) {
      const refreshError = error instanceof Error ? error.message : "unknown refresh error";
      setMessage(`${notice} The feed could not refresh: ${refreshError}`);
    }
  };

  return (
    <article className="pattern-card">
      <div className="pattern-card__heading">
        <div>
          <p className="section-kicker">Pattern FoodLog noticed</p>
          <h3>{question.prompt}</h3>
        </div>
        {question.pattern_observation_started_at && question.pattern_observation_ended_at ? (
          <p className="pattern-window">
            Evidence from {formatObservationDate(question.pattern_observation_started_at)} to{" "}
            {formatObservationDate(question.pattern_observation_ended_at)}
          </p>
        ) : null}
      </div>
      <p className="pattern-claim">{question.tentative_claim}</p>
      <p>{question.reason}</p>
      {question.pattern_uncertainty ? (
        <p className="pattern-uncertainty">
          <strong>What may weaken this:</strong> {question.pattern_uncertainty}
        </p>
      ) : null}

      <details className="pattern-evidence-details">
        <summary>Review the evidence behind this observation</summary>
        <div className="pattern-evidence-grid">
          <section>
            <h4>Supporting examples ({question.pattern_supporting_examples.length})</h4>
            <EvidenceExamples
              examples={question.pattern_supporting_examples}
              emptyLabel="No dated supporting example was persisted."
            />
          </section>
          <section>
            <h4>Counterexamples ({question.pattern_counterexamples.length})</h4>
            <EvidenceExamples
              examples={question.pattern_counterexamples}
              emptyLabel="No counterexample was found in this observation window."
            />
          </section>
        </div>
        <p className="pattern-provenance">
          Prompt: {question.pattern_prompt_version ?? "legacy"}
          {question.predecessor_question_id
            ? ` · Revisited after ${question.predecessor_question_id}`
            : ""}
        </p>
      </details>

      <div className="feedback-actions" aria-label="Pattern response">
        <button type="button" onClick={() => respond("confirm")} disabled={busy}>
          Yes, that is accurate
        </button>
        <button
          type="button"
          className="button--quiet"
          onClick={() => setMode((current) => current === "correct" ? undefined : "correct")}
          disabled={busy}
        >
          Not quite — correct it
        </button>
        <button
          type="button"
          className="button--quiet"
          onClick={() => setMode((current) => current === "reject" ? undefined : "reject")}
          disabled={busy}
        >
          No, this is not a pattern
        </button>
      </div>

      {mode === "correct" ? (
        <div className="feedback-form">
          <label>
            What is the accurate pattern?
            <textarea
              value={correction}
              onChange={(event) => setCorrection(event.target.value)}
              maxLength={500}
              rows={3}
            />
          </label>
          <label>
            Optional context
            <textarea
              value={explanation}
              onChange={(event) => setExplanation(event.target.value)}
              maxLength={4000}
              rows={3}
            />
          </label>
          <div className="button-row button-row--compact">
            <button type="button" onClick={() => respond("correct")} disabled={busy}>
              Save exact correction
            </button>
            <button type="button" className="button--quiet" onClick={() => setMode(undefined)} disabled={busy}>
              Cancel
            </button>
          </div>
        </div>
      ) : null}

      {mode === "reject" ? (
        <div className="feedback-form feedback-form--discard">
          <p>
            Rejecting this does not erase the evidence. The same claim stays suppressed
            unless enough new examples appear later.
          </p>
          <label>
            Optional reason
            <textarea
              value={explanation}
              onChange={(event) => setExplanation(event.target.value)}
              maxLength={4000}
              rows={3}
            />
          </label>
          <div className="button-row button-row--compact">
            <button type="button" className="button--danger" onClick={() => respond("reject")} disabled={busy}>
              Reject this pattern
            </button>
            <button type="button" className="button--quiet" onClick={() => setMode(undefined)} disabled={busy}>
              Cancel
            </button>
          </div>
        </div>
      ) : null}

      {message ? <p className="form-message" role="status">{message}</p> : null}
    </article>
  );
}
