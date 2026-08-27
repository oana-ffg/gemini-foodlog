import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  ApiError,
  answerQuestion,
  getConsentPreferences,
  listJournal,
  listMealRevisions,
  listOpenQuestions,
  loadCaptureImage,
  provisionAccount,
  recordLaunchMailConsent,
  submitMealFeedback,
  type Account,
  type ClarificationQuestion,
  type ConsentPreferences,
  type MealEntry,
  type MealRevision,
  type MealStatus,
} from "./api";
import { SessionControls, useAuth } from "./auth";
import { CapacityWaitlist, LaunchMailConsentControls } from "./ConsentControls";
import {
  clearSignupLaunchMailIntent,
  readSignupLaunchMailIntent,
} from "./signupIntent";

interface JournalCardProps {
  entry: MealEntry;
  onChanged: () => Promise<void>;
}

function StatusBadge({ status }: { status: MealStatus }) {
  return <span className={`status status--${status}`}>{status}</span>;
}

function RevisionHistory({ mealId, revisionCount }: { mealId: string; revisionCount: number }) {
  const [visible, setVisible] = useState(false);
  const [revisions, setRevisions] = useState<MealRevision[]>();
  const [message, setMessage] = useState<string>();

  const toggle = async () => {
    const nextVisible = !visible;
    setVisible(nextVisible);
    if (!nextVisible || revisions) return;
    setMessage("Loading history…");
    try {
      setRevisions(await listMealRevisions(mealId));
      setMessage(undefined);
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Could not load history");
    }
  };

  return (
    <div className="revision-history">
      <button type="button" className="text-button" onClick={toggle}>
        {visible ? "Hide" : "View"} revision history ({revisionCount})
      </button>
      {visible ? (
        <div className="revision-list">
          {message ? <p className="form-message">{message}</p> : null}
          {revisions?.map((revision) => (
            <article key={revision.id} className="revision">
              <div className="entry-meta">
                <strong>
                  {revision.source === "inference"
                    ? "Original inference"
                    : `Feedback revision ${revision.number}`}
                </strong>
                <StatusBadge status={revision.status} />
              </div>
              <h4>{revision.inference.title}</h4>
              <p>{revision.inference.rationale}</p>
            </article>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function JournalCard({ entry, onChanged }: JournalCardProps) {
  const [imageUrl, setImageUrl] = useState<string>();
  const [correcting, setCorrecting] = useState(false);
  const [actualMeal, setActualMeal] = useState("");
  const [explanation, setExplanation] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string>();

  useEffect(() => {
    let active = true;
    let objectUrl: string | undefined;
    loadCaptureImage(entry.capture_id)
      .then((url) => {
        objectUrl = url;
        if (active) setImageUrl(url);
      })
      .catch(() => undefined);
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [entry.capture_id]);

  const confirm = async () => {
    setBusy(true);
    setMessage("Saving confirmation…");
    try {
      await submitMealFeedback(entry.id, { kind: "confirm" }, crypto.randomUUID());
      await onChanged();
      setMessage("Confirmed. The original inference remains in the history.");
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Could not save confirmation");
    } finally {
      setBusy(false);
    }
  };

  const correct = async () => {
    setBusy(true);
    setMessage("Saving correction…");
    try {
      const trimmedMeal = actualMeal.trim();
      const trimmedExplanation = explanation.trim();
      await submitMealFeedback(
        entry.id,
        {
          kind: "correct",
          actual_meal: trimmedMeal || undefined,
          explanation: trimmedExplanation || undefined,
        },
        crypto.randomUUID(),
      );
      setCorrecting(false);
      setActualMeal("");
      setExplanation("");
      await onChanged();
      setMessage(
        trimmedMeal
          ? "Correction saved with the original inference intact."
          : "Marked wrong and unresolved. You can add the meal later.",
      );
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Could not save correction");
    } finally {
      setBusy(false);
    }
  };

  return (
    <article className="journal-card">
      {imageUrl ? <img src={imageUrl} alt="Captured kitchen evidence" /> : null}
      <div className="journal-card__body">
        <div className="entry-meta">
          <div className="badge-row">
            <span className={`confidence confidence--${entry.confidence}`}>
              {entry.confidence}
            </span>
            <StatusBadge status={entry.status} />
          </div>
          <time dateTime={entry.created_at}>
            {new Date(entry.created_at).toLocaleString()}
          </time>
        </div>
        <h3>{entry.title}</h3>
        <p>{entry.rationale}</p>
        <details>
          <summary>Evidence and alternatives</summary>
          <h4>Observed</h4>
          <ul>{entry.observations.map((item) => <li key={item}>{item}</li>)}</ul>
          {entry.alternatives.length > 0 ? (
            <>
              <h4>Alternatives</h4>
              <ul>{entry.alternatives.map((item) => <li key={item}>{item}</li>)}</ul>
            </>
          ) : null}
        </details>

        <div className="feedback-actions" aria-label="Meal feedback">
          {entry.status === "provisional" ? (
            <button type="button" onClick={confirm} disabled={busy}>Looks right</button>
          ) : null}
          <button
            type="button"
            className="button--quiet"
            onClick={() => setCorrecting((current) => !current)}
            disabled={busy}
          >
            {entry.status === "contradicted" ? "Add the actual meal" : "Correct it"}
          </button>
        </div>

        {correcting ? (
          <div className="feedback-form">
            <p>
              The complete rationale above will remain in history. Give either answer,
              both, or leave both blank to mark this wrong but unresolved.
            </p>
            <label>
              What was it actually?
              <input
                value={actualMeal}
                onChange={(event) => setActualMeal(event.target.value)}
                maxLength={200}
              />
            </label>
            <label>
              Why was the reasoning wrong, and how could FoodLog tell next time?
              <textarea
                value={explanation}
                onChange={(event) => setExplanation(event.target.value)}
                maxLength={2000}
                rows={4}
              />
            </label>
            <div className="button-row button-row--compact">
              <button type="button" onClick={correct} disabled={busy}>Save correction</button>
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
        {message ? <p className="form-message" role="status">{message}</p> : null}
        <RevisionHistory
          key={entry.revision_number}
          mealId={entry.id}
          revisionCount={entry.revision_number}
        />
      </div>
    </article>
  );
}

interface QuestionCardProps {
  question: ClarificationQuestion;
  onChanged: () => Promise<void>;
}

function QuestionCard({ question, onChanged }: QuestionCardProps) {
  const [answer, setAnswer] = useState("");
  const [learningTip, setLearningTip] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string>();

  const submit = async () => {
    const trimmedAnswer = answer.trim();
    if (!trimmedAnswer) {
      setMessage("Please tell FoodLog what the meal or ingredient was.");
      return;
    }
    setBusy(true);
    setMessage("Applying your answer…");
    try {
      await answerQuestion(
        question.id,
        trimmedAnswer,
        learningTip.trim() || undefined,
        crypto.randomUUID(),
      );
      await onChanged();
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Could not save the answer");
      setBusy(false);
    }
  };

  return (
    <article className="question-card">
      <div>
        <p className="section-kicker">Needs your context</p>
        <h3>{question.prompt}</h3>
        <p>{question.reason}</p>
      </div>
      <div className="question-form">
        <label>
          Your answer
          <input
            value={answer}
            onChange={(event) => setAnswer(event.target.value)}
            maxLength={200}
          />
        </label>
        <label>
          Optional tip for next time
          <textarea
            value={learningTip}
            onChange={(event) => setLearningTip(event.target.value)}
            maxLength={2000}
            rows={3}
          />
        </label>
        <button type="button" onClick={submit} disabled={busy}>
          {busy ? "Saving…" : "Answer and update journal"}
        </button>
        {message ? <p className="form-message" role="status">{message}</p> : null}
      </div>
    </article>
  );
}

function App() {
  const { user } = useAuth();
  const firebaseUid = user?.uid;
  const [account, setAccount] = useState<Account>();
  const [consentPreferences, setConsentPreferences] = useState<ConsentPreferences>();
  const [capacityReached, setCapacityReached] = useState(false);
  const [journal, setJournal] = useState<MealEntry[]>([]);
  const [questions, setQuestions] = useState<ClarificationQuestion[]>([]);
  const [loadMessage, setLoadMessage] = useState("Loading your private journal…");

  const refreshWorkspace = useCallback(async () => {
    setLoadMessage("Loading your private journal…");
    try {
      const currentAccount = await provisionAccount();
      let currentConsent = await getConsentPreferences();
      const signupIntent = firebaseUid
        ? readSignupLaunchMailIntent(firebaseUid)
        : undefined;
      if (firebaseUid && signupIntent !== undefined) {
        if (currentConsent.launch_mail_opt_in === null) {
          const consent = await recordLaunchMailConsent(signupIntent);
          currentConsent = {
            ...currentConsent,
            launch_mail_opt_in: consent.granted,
            launch_mail_policy_version: consent.policy_version,
            launch_mail_updated_at: consent.created_at,
          };
        }
        clearSignupLaunchMailIntent(firebaseUid);
      }
      const [entries, openQuestions] = await Promise.all([
        listJournal(),
        listOpenQuestions(),
      ]);
      setJournal(entries);
      setAccount(currentAccount);
      setConsentPreferences(currentConsent);
      setQuestions(openQuestions);
      setCapacityReached(false);
      setLoadMessage("");
    } catch (error: unknown) {
      if (
        error instanceof ApiError
        && error.status === 409
        && error.message === "signup_capacity_exhausted"
      ) {
        setAccount(undefined);
        setJournal([]);
        setQuestions([]);
        setConsentPreferences(await getConsentPreferences());
        setCapacityReached(true);
        setLoadMessage("");
        return;
      }
      throw error;
    }
  }, [firebaseUid]);

  useEffect(() => {
    refreshWorkspace()
      .catch((error: unknown) => {
        setLoadMessage(error instanceof Error ? error.message : "The journal is unavailable.");
      });
  }, [refreshWorkspace]);

  if (capacityReached && consentPreferences) {
    return (
      <CapacityWaitlist
        preferences={consentPreferences}
        onChanged={refreshWorkspace}
      />
    );
  }

  return (
    <main className="app-shell">
      <header className="hero">
        <SessionControls />
        <p className="eyebrow">All Things Agentic Hackathon 2026</p>
        <h1>Your food journal,<br />without the diary ritual.</h1>
        <p className="hero__copy">
          Gemini FoodLog watches ordinary kitchen activity, explains what it
          inferred, and gets better when you correct it.
        </p>
        <div className="usage">
          <span>Local vertical slice</span>
          <strong>
            {account?.accepted_image_count ?? 0} / {account?.entitlement_mode === "unlimited"
              ? "Unlimited"
              : account?.trial_image_limit ?? 200} images
          </strong>
        </div>
        <Link className="camera-link" to="/camera">Open the phone camera page</Link>
      </header>

      {consentPreferences ? (
        <LaunchMailConsentControls
          preferences={consentPreferences}
          onChanged={refreshWorkspace}
        />
      ) : null}

      {loadMessage ? <p className="empty-state" role="status">{loadMessage}</p> : null}

      <section className="questions" aria-labelledby="questions-title">
        <div className="section-heading">
          <div>
            <p className="section-kicker">Clarification inbox</p>
            <h2 id="questions-title">Questions worth answering</h2>
          </div>
          <span className="question-count">{questions.length} open</span>
        </div>
        {questions.length === 0 ? (
          <p className="empty-state">Nothing needs your attention right now.</p>
        ) : questions.map((question) => (
          <QuestionCard key={question.id} question={question} onChanged={refreshWorkspace} />
        ))}
      </section>

      <section className="journal" aria-labelledby="journal-title">
        <div className="section-heading">
          <div>
            <p className="section-kicker">Evidence-aware timeline</p>
            <h2 id="journal-title">What FoodLog thinks happened</h2>
          </div>
          <button type="button" className="button--quiet" onClick={refreshWorkspace}>
            Refresh
          </button>
        </div>
        {journal.length === 0 ? (
          <p className="empty-state">No kitchen event has been analysed yet.</p>
        ) : journal.map((entry) => (
          <JournalCard key={entry.id} entry={entry} onChanged={refreshWorkspace} />
        ))}
      </section>
    </main>
  );
}

export default App;
