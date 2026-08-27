import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  getConsentPreferences,
  getKnowledgePage,
  getPurchase,
  listActivities,
  listAuditEvents,
  listCameras,
  listCaptureInventory,
  listContextNotes,
  listFeedbackInventory,
  listKnowledge,
  listMealRevisions,
  listPurchases,
  listQuestions,
  provisionAccount,
  type Account,
  type AuditEvent,
  type Camera,
  type CaptureInventory,
  type ClarificationQuestion,
  type ConsentPreferences,
  type FeedbackInventory,
  type KnowledgePageHistory,
  type MealEntry,
  type MealRevision,
  type PurchaseDetail,
  type UserContextNote,
} from "./api";
import { SessionControls } from "./auth";

interface AccountDataSnapshot {
  account: Account;
  consent: ConsentPreferences;
  cameras: Camera[];
  captures: CaptureInventory[];
  activities: MealEntry[];
  meal_revisions: Record<string, MealRevision[]>;
  questions: ClarificationQuestion[];
  feedback: FeedbackInventory;
  context_notes: UserContextNote[];
  knowledge: KnowledgePageHistory[];
  purchases: PurchaseDetail[];
  audit_events: AuditEvent[];
}

function StoredCollection({
  title,
  count,
  value,
  note,
}: {
  title: string;
  count: number;
  value: unknown;
  note?: string;
}) {
  return (
    <details className="stored-data-collection">
      <summary><strong>{title}</strong><span>{count} record{count === 1 ? "" : "s"}</span></summary>
      {note ? <p>{note}</p> : null}
      <pre>{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}

async function loadAccountData(): Promise<AccountDataSnapshot> {
  const [
    account,
    consent,
    cameras,
    captures,
    activities,
    openQuestions,
    answeredQuestions,
    supersededQuestions,
    feedback,
    contextNotes,
    knowledgePages,
    purchaseSummaries,
    auditEvents,
  ] = await Promise.all([
    provisionAccount(),
    getConsentPreferences(),
    listCameras(),
    listCaptureInventory(200),
    listActivities(),
    listQuestions("open"),
    listQuestions("answered"),
    listQuestions("superseded"),
    listFeedbackInventory(200),
    listContextNotes(true),
    listKnowledge(true),
    listPurchases(50),
    listAuditEvents(200),
  ]);
  const [revisionLists, knowledge, purchases] = await Promise.all([
    Promise.all(activities.map((activity) => listMealRevisions(activity.id))),
    Promise.all(knowledgePages.map((page) => getKnowledgePage(page.id))),
    Promise.all(purchaseSummaries.map((purchase) => getPurchase(purchase.id))),
  ]);
  return {
    account,
    consent,
    cameras,
    captures,
    activities,
    meal_revisions: Object.fromEntries(
      activities.map((activity, index) => [activity.id, revisionLists[index] ?? []]),
    ),
    questions: [...openQuestions, ...answeredQuestions, ...supersededQuestions],
    feedback,
    context_notes: contextNotes,
    knowledge,
    purchases,
    audit_events: auditEvents,
  };
}

export default function AccountDataPage() {
  const [snapshot, setSnapshot] = useState<AccountDataSnapshot>();
  const [message, setMessage] = useState("Loading your stored account data…");
  const requestIdRef = useRef(0);

  const refresh = useCallback(() => {
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    setMessage("Loading your stored account data…");
    void loadAccountData().then(
      (value) => {
        if (requestIdRef.current !== requestId) return;
        setSnapshot(value);
        setMessage("");
      },
      (error: unknown) => {
        if (requestIdRef.current !== requestId) return;
        setMessage(error instanceof Error ? error.message : "Stored account data is unavailable.");
      },
    );
  }, []);

  useEffect(() => {
    refresh();
    return () => { requestIdRef.current += 1; };
  }, [refresh]);

  const counts = snapshot
    ? [
      ["images", snapshot.captures.length],
      ["activities", snapshot.activities.length],
      ["purchases", snapshot.purchases.length],
      ["questions", snapshot.questions.length],
      ["feedback", snapshot.feedback.meal_feedback.length + snapshot.feedback.question_responses.length],
      ["knowledge pages", snapshot.knowledge.length],
    ] as const
    : [];

  return (
    <main className="data-page">
      <header className="data-page__header">
        <div>
          <p className="eyebrow">Private owner-only inventory</p>
          <h1>Your stored FoodLog data</h1>
          <p>Inspect the records the application currently exposes for your account. Nothing here is public.</p>
        </div>
        <div className="data-page__account">
          <SessionControls />
          <Link to="/">Back to journal</Link>
        </div>
      </header>

      {message ? <p className="empty-state" role="status">{message}</p> : null}
      {snapshot ? (
        <>
          <section className="data-overview" aria-labelledby="data-overview-title">
            <div>
              <p className="section-kicker">Usage and entitlement</p>
              <h2 id="data-overview-title">
                {snapshot.account.entitlement_mode === "unlimited"
                  ? `${snapshot.account.accepted_image_count} images · unlimited`
                  : `${snapshot.account.accepted_image_count} of ${snapshot.account.trial_image_limit} images used`}
              </h2>
              <p>Account {snapshot.account.id}</p>
            </div>
            <dl>
              {counts.map(([label, count]) => (
                <div key={label}><dt>{label}</dt><dd>{count}</dd></div>
              ))}
            </dl>
          </section>

          <section className="stored-data" aria-labelledby="stored-data-title">
            <div className="section-heading">
              <div>
                <p className="section-kicker">Exact API records</p>
                <h2 id="stored-data-title">Browse the current inventory</h2>
              </div>
              <button type="button" className="button--quiet" onClick={refresh}>Refresh</button>
            </div>
            <p className="section-intro">
              Credential verifiers, idempotency hashes, private object paths, service secrets, and other tenants are intentionally excluded. Portable ZIP export of retained binaries, raw mail, and traces is tracked separately from this browser view.
            </p>
            <StoredCollection title="Account and image entitlement" count={1} value={snapshot.account} />
            <StoredCollection title="Consent and waitlist state" count={1} value={snapshot.consent} />
            <StoredCollection title="Camera sources" count={snapshot.cameras.length} value={snapshot.cameras} />
            <StoredCollection title="Stored image metadata" count={snapshot.captures.length} value={snapshot.captures} note="Up to the complete 200-image public trial is shown. Original private images remain available from their activity cards." />
            <StoredCollection title="Kitchen activities and meals" count={snapshot.activities.length} value={snapshot.activities} />
            <StoredCollection title="Immutable meal revisions" count={Object.values(snapshot.meal_revisions).reduce((total, revisions) => total + revisions.length, 0)} value={snapshot.meal_revisions} />
            <StoredCollection title="Agent questions and answers" count={snapshot.questions.length} value={snapshot.questions} />
            <StoredCollection title="Raw feedback and question responses" count={snapshot.feedback.meal_feedback.length + snapshot.feedback.question_responses.length} value={snapshot.feedback} />
            <StoredCollection title="Temporary context notes" count={snapshot.context_notes.length} value={snapshot.context_notes} />
            <StoredCollection title="Household knowledge with provenance" count={snapshot.knowledge.length} value={snapshot.knowledge} />
            <StoredCollection title="Normalized purchase evidence" count={snapshot.purchases.length} value={snapshot.purchases} />
            <StoredCollection title="User-visible security and processing audit" count={snapshot.audit_events.length} value={snapshot.audit_events} note="The newest 200 user-visible audit records are shown." />
          </section>
        </>
      ) : null}
    </main>
  );
}
