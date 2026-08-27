import { useState } from "react";
import {
  submitMealFeedback,
  type MealCorrection,
  type MealEntry,
  type MealFeedbackResult,
} from "./api";

type CorrectionScope = "meal" | "component";
type LearningUse = "event_only" | "reusable" | "insufficient_information";

interface FeedbackActionState {
  canConfirm: boolean;
  canCorrect: boolean;
  canDiscard: boolean;
  correctionLabel: string;
}

export function feedbackActionState(entry: MealEntry): FeedbackActionState {
  const hypothesis = entry.activity_hypothesis;
  const kind = hypothesis?.kind ?? (
    entry.title === "Unknown kitchen activity" ? "unknown_activity" : "tentative_meal"
  );
  const allowedActions = new Set(
    hypothesis?.allowed_actions ?? ["confirm_guess", "correct", "discard_not_cooking"],
  );
  const isDiscarded = entry.status === "not_cooking";
  const correctionLabel = isDiscarded
    ? "Reclassify as cooking"
    : kind === "unknown_activity"
      ? "Tell FoodLog what this was"
      : kind === "likely_non_cooking"
        ? "Correct classification"
        : entry.status === "contradicted"
          ? "Add the actual meal"
          : "Correct it";

  return {
    canConfirm: (
      entry.status === "provisional"
      && kind === "tentative_meal"
      && allowedActions.has("confirm_guess")
    ),
    canCorrect: isDiscarded || allowedActions.has("correct"),
    canDiscard: !isDiscarded && allowedActions.has("discard_not_cooking"),
    correctionLabel,
  };
}

function splitList(value: string): string[] {
  return value
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function resultMessage(
  result: MealFeedbackResult,
  action: "confirm" | "correct" | "not_cooking",
  target?: string,
): string {
  if (action === "confirm") {
    return `Confirmed in revision ${result.revision.number}; the original inference remains in history.`;
  }
  if (action === "not_cooking") {
    return `Discarded as not cooking in revision ${result.revision.number}. The evidence remains under discarded activity.`;
  }
  const learning = result.learning_outcome === "knowledge_applied"
    ? " Household knowledge was updated from your explicit reusable guidance."
    : result.learning_outcome === "insufficient_information"
      ? " The explanation was retained as insufficient information, not a household rule."
      : " No reusable household rule was inferred.";
  return `${target ?? "Correction"} saved in revision ${result.revision.number}.${learning}`;
}

export function CorrectionSummary({ correction }: { correction: MealCorrection }) {
  if (correction.scope === "meal") {
    return <p className="correction-summary"><strong>Whole meal:</strong> {correction.title}</p>;
  }
  if (correction.scope === "component") {
    return (
      <div className="correction-summary">
        <p><strong>Component {correction.component_index + 1}:</strong> {correction.replacement.name}</p>
        {correction.replacement.ingredients.length > 0 ? (
          <small>Ingredients: {correction.replacement.ingredients.join(", ")}</small>
        ) : null}
        {correction.replacement.preparation_methods.length > 0 ? (
          <small>Preparation: {correction.replacement.preparation_methods.join(", ")}</small>
        ) : null}
      </div>
    );
  }
  if (correction.scope === "ingredient") {
    return (
      <p className="correction-summary">
        <strong>Ingredient {correction.ingredient_index + 1} in component {correction.component_index + 1}:</strong>{" "}
        {correction.replacement}
      </p>
    );
  }
  return (
    <p className="correction-summary">
      <strong>Preparation method {correction.preparation_method_index + 1} in component {correction.component_index + 1}:</strong>{" "}
      {correction.replacement}
    </p>
  );
}

interface MealFeedbackControlsProps {
  entry: MealEntry;
  onChanged: () => Promise<void>;
  onNotice: (message: string) => void;
}

export default function MealFeedbackControls({
  entry,
  onChanged,
  onNotice,
}: MealFeedbackControlsProps) {
  const actions = feedbackActionState(entry);
  const [correcting, setCorrecting] = useState(false);
  const [discarding, setDiscarding] = useState(false);
  const [scope, setScope] = useState<CorrectionScope>("meal");
  const [actualMeal, setActualMeal] = useState("");
  const [componentIndex, setComponentIndex] = useState(0);
  const [componentName, setComponentName] = useState(entry.components[0]?.name ?? "");
  const [ingredients, setIngredients] = useState(
    entry.components[0]?.ingredients.join(", ") ?? "",
  );
  const [preparationMethods, setPreparationMethods] = useState(
    entry.components[0]?.preparation_methods.join(", ") ?? "",
  );
  const [explanation, setExplanation] = useState("");
  const [learningUse, setLearningUse] = useState<LearningUse>("event_only");
  const [discardReason, setDiscardReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string>();

  const chooseComponent = (index: number) => {
    const component = entry.components[index];
    setComponentIndex(index);
    setComponentName(component?.name ?? "");
    setIngredients(component?.ingredients.join(", ") ?? "");
    setPreparationMethods(component?.preparation_methods.join(", ") ?? "");
  };

  const confirm = async () => {
    setBusy(true);
    setMessage("Saving confirmation…");
    try {
      const result = await submitMealFeedback(
        entry.id,
        { kind: "confirm" },
        crypto.randomUUID(),
      );
      const notice = resultMessage(result, "confirm");
      onNotice(notice);
      await onChanged();
      setMessage(notice);
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Could not save confirmation");
    } finally {
      setBusy(false);
    }
  };

  const saveCorrection = async () => {
    const trimmedExplanation = explanation.trim();
    let correction: MealCorrection;
    let target: string;
    if (scope === "meal") {
      const title = actualMeal.trim();
      if (!title) {
        setMessage("Tell FoodLog what this meal or activity actually was.");
        return;
      }
      correction = { scope: "meal", title };
      target = `Whole meal corrected to “${title}”`;
    } else {
      const name = componentName.trim();
      if (!name) {
        setMessage("Give the corrected component a name.");
        return;
      }
      correction = {
        scope: "component",
        component_index: componentIndex,
        replacement: {
          name,
          ingredients: splitList(ingredients),
          preparation_methods: splitList(preparationMethods),
        },
      };
      target = `Component ${componentIndex + 1} corrected to “${name}”`;
    }
    if (learningUse !== "event_only" && !trimmedExplanation) {
      setMessage("Explain the distinction before deciding how FoodLog should learn from it.");
      return;
    }

    setBusy(true);
    setMessage("Saving an immutable correction…");
    try {
      const result = await submitMealFeedback(
        entry.id,
        {
          kind: "correct",
          correction,
          base_revision_number: entry.revision_number,
          explanation: trimmedExplanation || undefined,
          learning_disposition: learningUse === "event_only" ? undefined : learningUse,
        },
        crypto.randomUUID(),
      );
      const notice = resultMessage(result, "correct", target);
      onNotice(notice);
      setCorrecting(false);
      await onChanged();
      setMessage(notice);
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Could not save correction");
    } finally {
      setBusy(false);
    }
  };

  const discard = async () => {
    setBusy(true);
    setMessage("Saving the not-cooking disposition…");
    try {
      const result = await submitMealFeedback(
        entry.id,
        {
          kind: "not_cooking",
          explanation: discardReason.trim() || undefined,
        },
        crypto.randomUUID(),
      );
      const notice = resultMessage(result, "not_cooking");
      onNotice(notice);
      setDiscarding(false);
      await onChanged();
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Could not discard this activity");
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="feedback-actions" aria-label="Activity feedback">
        {actions.canConfirm ? (
          <button type="button" onClick={confirm} disabled={busy}>Looks right</button>
        ) : null}
        {actions.canCorrect ? (
          <button
            type="button"
            className="button--quiet"
            onClick={() => {
              setCorrecting((current) => !current);
              setDiscarding(false);
            }}
            disabled={busy}
          >
            {actions.correctionLabel}
          </button>
        ) : null}
        {actions.canDiscard ? (
          <button
            type="button"
            className="button--quiet"
            onClick={() => {
              setDiscarding((current) => !current);
              setCorrecting(false);
            }}
            disabled={busy}
          >
            Discard as not cooking
          </button>
        ) : null}
      </div>

      {correcting ? (
        <div className="feedback-form">
          <p>
            The original inference remains intact. This creates a new revision against
            revision {entry.revision_number} and changes only the selected scope.
          </p>
          {entry.components.length > 0 ? (
            <label>
              What needs changing?
              <select
                value={scope}
                onChange={(event) => setScope(event.target.value as CorrectionScope)}
              >
                <option value="meal">The whole meal or activity</option>
                <option value="component">One component only</option>
              </select>
            </label>
          ) : null}

          {scope === "meal" || entry.components.length === 0 ? (
            <label>
              What was it actually?
              <input
                value={actualMeal}
                onChange={(event) => setActualMeal(event.target.value)}
                maxLength={200}
              />
            </label>
          ) : (
            <>
              <label>
                Component to replace
                <select
                  value={componentIndex}
                  onChange={(event) => chooseComponent(Number(event.target.value))}
                >
                  {entry.components.map((component, index) => (
                    <option key={`${component.name}-${index}`} value={index}>
                      {index + 1}. {component.name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Correct component name
                <input
                  value={componentName}
                  onChange={(event) => setComponentName(event.target.value)}
                  maxLength={200}
                />
              </label>
              <label>
                Ingredients, comma separated
                <input
                  value={ingredients}
                  onChange={(event) => setIngredients(event.target.value)}
                  maxLength={2000}
                />
              </label>
              <label>
                Preparation methods, comma separated
                <input
                  value={preparationMethods}
                  onChange={(event) => setPreparationMethods(event.target.value)}
                  maxLength={2000}
                />
              </label>
            </>
          )}

          <label>
            Why was the reasoning wrong, and how could FoodLog tell next time?
            <textarea
              value={explanation}
              onChange={(event) => setExplanation(event.target.value)}
              maxLength={2000}
              rows={4}
            />
          </label>
          <label>
            How should FoodLog use that explanation?
            <select
              value={learningUse}
              onChange={(event) => setLearningUse(event.target.value as LearningUse)}
            >
              <option value="event_only">This event only; do not invent a rule</option>
              <option value="reusable">This is reusable household guidance</option>
              <option value="insufficient_information">Retain it, but there is not enough information to learn</option>
            </select>
          </label>
          <div className="button-row button-row--compact">
            <button type="button" onClick={saveCorrection} disabled={busy}>
              Save correction
            </button>
            <button
              type="button"
              className="button--quiet"
              onClick={() => setCorrecting(false)}
              disabled={busy}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : null}

      {discarding ? (
        <div className="feedback-form feedback-form--discard">
          <p>
            This removes the event from the food journal, but preserves its images,
            inference, reason, and revision history under discarded activity.
          </p>
          <label>
            Optional reason
            <textarea
              value={discardReason}
              onChange={(event) => setDiscardReason(event.target.value)}
              maxLength={2000}
              rows={3}
            />
          </label>
          <div className="button-row button-row--compact">
            <button type="button" className="button--danger" onClick={discard} disabled={busy}>
              Confirm not cooking
            </button>
            <button
              type="button"
              className="button--quiet"
              onClick={() => setDiscarding(false)}
              disabled={busy}
            >
              Cancel
            </button>
          </div>
        </div>
      ) : null}

      {message ? <p className="form-message" role="status">{message}</p> : null}
    </>
  );
}
