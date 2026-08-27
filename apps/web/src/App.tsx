import { useCallback, useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import {
  ApiError,
  AuthenticationRequiredError,
  answerQuestion,
  getConsentPreferences,
  listActivities,
  listJournal,
  listMealRevisions,
  listOpenQuestions,
  listProcessing,
  listPurchases,
  provisionAccount,
  recordLaunchMailConsent,
  type Account,
  type CaptureProcessing,
  type ClarificationQuestion,
  type ConsentPreferences,
  type MealEntry,
  type MealRevision,
  type MealStatus,
} from "./api";
import { ActivityImageViewer, ActivityRationale } from "./ActivityDetail";
import MealFeedbackControls, { CorrectionSummary } from "./MealFeedbackControls";
import { SessionControls, useAuth } from "./auth";
import { CapacityWaitlist, LaunchMailConsentControls } from "./ConsentControls";
import {
  clearSignupLaunchMailIntent,
  readSignupLaunchMailIntent,
} from "./signupIntent";
import { chronologicalJournal, mealOccurrence } from "./journal";
import { SystemStatus, type PurchaseContextState } from "./SystemStatus";

interface JournalCardProps {
  entry: MealEntry;
  onChanged: () => Promise<void>;
  onNotice: (message: string) => void;
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
              <ActivityRationale
                inference={revision.inference}
                hypothesis={revision.activity_hypothesis}
              />
              {revision.correction ? (
                <CorrectionSummary correction={revision.correction} />
              ) : null}
              {revision.base_revision_number !== null ? (
                <small className="revision__base">
                  Changed from revision {revision.base_revision_number}
                </small>
              ) : null}
            </article>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function JournalCard({ entry, onChanged, onNotice }: JournalCardProps) {
  const captureIds = entry.activity_hypothesis?.source_capture_ids ?? [entry.capture_id];
  const [selectedCaptureId, setSelectedCaptureId] = useState(entry.capture_id);
  const visibleCaptureId = captureIds.includes(selectedCaptureId)
    ? selectedCaptureId
    : captureIds[0] ?? entry.capture_id;

  return (
    <article className="journal-card">
      <ActivityImageViewer
        captureIds={captureIds}
        selectedCaptureId={visibleCaptureId}
        onSelectCapture={setSelectedCaptureId}
      />
      <div className="journal-card__body">
        <div className="entry-meta">
          <div className="badge-row">
            <span className={`confidence confidence--${entry.confidence}`}>
              {entry.confidence}
            </span>
            <StatusBadge status={entry.status} />
          </div>
          <time dateTime={mealOccurrence(entry)}>
            {new Date(mealOccurrence(entry)).toLocaleString()}
          </time>
        </div>
        <h3>{entry.title}</h3>
        <p>{entry.rationale}</p>
        <ActivityRationale
          inference={entry}
          hypothesis={entry.activity_hypothesis}
          onSelectCapture={setSelectedCaptureId}
        />

        <MealFeedbackControls
          entry={entry}
          onChanged={onChanged}
          onNotice={onNotice}
        />
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
  const [discardedActivities, setDiscardedActivities] = useState<MealEntry[]>([]);
  const [questions, setQuestions] = useState<ClarificationQuestion[]>([]);
  const [processing, setProcessing] = useState<CaptureProcessing[]>();
  const [processingUnavailable, setProcessingUnavailable] = useState(false);
  const [purchaseContext, setPurchaseContext] = useState<PurchaseContextState>("loading");
  const [sessionStale, setSessionStale] = useState(false);
  const [loadMessage, setLoadMessage] = useState("Loading your private journal…");
  const [journalNotice, setJournalNotice] = useState<string>();
  const orderedJournal = useMemo(() => chronologicalJournal(journal), [journal]);
  const orderedDiscardedActivities = useMemo(
    () => chronologicalJournal(discardedActivities),
    [discardedActivities],
  );

  const refreshWorkspace = useCallback(async () => {
    setLoadMessage("Loading your private journal…");
    setProcessing(undefined);
    setProcessingUnavailable(false);
    setPurchaseContext("loading");
    setSessionStale(false);
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
      const [entries, activities, openQuestions, processingResult, purchasesResult] = await Promise.all([
        listJournal(),
        listActivities("not_cooking"),
        listOpenQuestions(),
        listProcessing().then(
          (value) => ({ value }),
          (error: unknown) => ({ error }),
        ),
        listPurchases(1).then(
          (value) => ({ value }),
          (error: unknown) => ({ error }),
        ),
      ]);
      if ("error" in processingResult && processingResult.error instanceof AuthenticationRequiredError) {
        throw processingResult.error;
      }
      if ("error" in purchasesResult && purchasesResult.error instanceof AuthenticationRequiredError) {
        throw purchasesResult.error;
      }
      setJournal(entries);
      setDiscardedActivities(
        activities.filter((activity) => activity.status === "not_cooking"),
      );
      setAccount(currentAccount);
      setConsentPreferences(currentConsent);
      setQuestions(openQuestions);
      if ("value" in processingResult) {
        setProcessing(processingResult.value);
      } else {
        setProcessingUnavailable(true);
      }
      if ("value" in purchasesResult) {
        setPurchaseContext(purchasesResult.value.length > 0 ? "available" : "empty");
      } else {
        setPurchaseContext("unavailable");
      }
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
        setDiscardedActivities([]);
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
        setSessionStale(error instanceof AuthenticationRequiredError);
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
          <span>Private account</span>
          <strong>
            {account?.accepted_image_count ?? 0} / {account?.entitlement_mode === "unlimited"
              ? "Unlimited"
              : account?.trial_image_limit ?? 200} images
          </strong>
        </div>
        <nav className="hero-links" aria-label="FoodLog tools">
          <Link to="/context">Tell FoodLog something</Link>
          <Link to="/knowledge">Open the household wiki</Link>
          <Link to="/camera">Open the phone camera page</Link>
        </nav>
      </header>

      {consentPreferences ? (
        <LaunchMailConsentControls
          preferences={consentPreferences}
          onChanged={refreshWorkspace}
        />
      ) : null}

      {loadMessage ? <p className="empty-state" role="status">{loadMessage}</p> : null}

      <SystemStatus
        account={account}
        processing={processing}
        processingUnavailable={processingUnavailable}
        purchaseContext={purchaseContext}
        sessionStale={sessionStale}
      />

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
            <p className="section-kicker">Newest kitchen event first</p>
            <h2 id="journal-title">Your food timeline</h2>
          </div>
          <button type="button" className="button--quiet" onClick={refreshWorkspace}>
            Refresh
          </button>
        </div>
        {journalNotice ? <p className="journal-notice" role="status">{journalNotice}</p> : null}
        {journal.length === 0 ? (
          <p className="empty-state">No kitchen event has been analysed yet.</p>
        ) : orderedJournal.map((entry) => (
          <JournalCard
            key={entry.id}
            entry={entry}
            onChanged={refreshWorkspace}
            onNotice={setJournalNotice}
          />
        ))}
      </section>

      <section className="discarded-activities" aria-labelledby="discarded-title">
        <div className="section-heading">
          <div>
            <p className="section-kicker">Preserved, but not food</p>
            <h2 id="discarded-title">Discarded non-cooking activity</h2>
          </div>
          <span className="question-count">{discardedActivities.length} saved</span>
        </div>
        <p className="section-intro">
          These events stay out of your food timeline. Their evidence and revision history
          remain available so you can reclassify a mistake.
        </p>
        {discardedActivities.length === 0 ? (
          <p className="empty-state">No activity has been discarded as not cooking.</p>
        ) : orderedDiscardedActivities.map((entry) => (
          <JournalCard
            key={entry.id}
            entry={entry}
            onChanged={refreshWorkspace}
            onNotice={setJournalNotice}
          />
        ))}
      </section>
    </main>
  );
}

export default App;
