import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import {
  ApiError,
  correctKnowledge,
  getKnowledgePage,
  listKnowledge,
  retireKnowledge,
  teachKnowledge,
  type KnowledgePage as KnowledgePageRecord,
  type KnowledgePageHistory,
  type KnowledgeRevision,
} from "./api";
import { SessionControls } from "./auth";

function humanize(value: string): string {
  return value.replaceAll("_", " ");
}

function KnowledgeRevisionCard({ revision }: { revision: KnowledgeRevision }) {
  return (
    <article className="knowledge-revision">
      <div className="entry-meta">
        <strong>Revision {revision.number}</strong>
        <time dateTime={revision.created_at}>
          {new Date(revision.created_at).toLocaleString()}
        </time>
      </div>
      <div className="badge-row">
        <span className={`knowledge-lifecycle knowledge-lifecycle--${revision.lifecycle}`}>
          {humanize(revision.lifecycle)}
        </span>
        <span className="status">{humanize(revision.source)}</span>
        <span className="status">{revision.belief_strength} belief</span>
      </div>
      <p className="knowledge-revision__statement">{revision.statement}</p>
      <p><strong>Why this revision exists:</strong> {revision.reason}</p>
      <details>
        <summary>Provenance ({revision.evidence.length})</summary>
        <ul className="knowledge-evidence">
          {revision.evidence.map((evidence) => (
            <li key={`${evidence.kind}:${evidence.id}`}>
              <span>{humanize(evidence.role)}</span>
              <strong>{humanize(evidence.kind)}</strong>
              <code>{evidence.id}</code>
              {evidence.note ? <p>{evidence.note}</p> : null}
            </li>
          ))}
        </ul>
      </details>
    </article>
  );
}

interface KnowledgeDetailProps {
  history: KnowledgePageHistory;
  onChanged: (pageId: string) => Promise<void>;
}

export function KnowledgeDetail({ history, onChanged }: KnowledgeDetailProps) {
  const { page } = history;
  const [mode, setMode] = useState<"correct" | "retire">();
  const [statement, setStatement] = useState(page.statement);
  const [retirementReason, setRetirementReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string>();

  const cancel = () => {
    setMode(undefined);
    setStatement(page.statement);
    setRetirementReason("");
    setMessage(undefined);
  };

  const saveCorrection = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const corrected = statement.trim();
    if (!corrected) {
      setMessage("The corrected household knowledge cannot be empty.");
      return;
    }
    setBusy(true);
    setMessage("Saving a new immutable revision…");
    try {
      await correctKnowledge(
        page.id,
        corrected,
        page.current_revision_number,
        crypto.randomUUID(),
      );
      await onChanged(page.id);
      setMode(undefined);
      setMessage("Correction saved. The previous wording remains in history.");
    } catch (error: unknown) {
      if (error instanceof ApiError && error.status === 409) {
        await onChanged(page.id);
        setMessage("This page changed elsewhere. It has been refreshed; review before retrying.");
      } else {
        setMessage(error instanceof Error ? error.message : "Could not save the correction.");
      }
    } finally {
      setBusy(false);
    }
  };

  const retire = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setBusy(true);
    setMessage("Retiring this belief without erasing its history…");
    try {
      await retireKnowledge(
        page.id,
        page.current_revision_number,
        retirementReason.trim() || undefined,
        crypto.randomUUID(),
      );
      await onChanged(page.id);
      setMode(undefined);
      setMessage("Retired. The page and its provenance remain available in history.");
    } catch (error: unknown) {
      if (error instanceof ApiError && error.status === 409) {
        await onChanged(page.id);
        setMessage("This page changed elsewhere. It has been refreshed; review before retrying.");
      } else {
        setMessage(error instanceof Error ? error.message : "Could not retire this belief.");
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="knowledge-detail" aria-labelledby="knowledge-detail-title">
      <div className="knowledge-detail__header">
        <div>
          <p className="section-kicker">Current household belief</p>
          <h2 id="knowledge-detail-title">{page.title}</h2>
        </div>
        <div className="badge-row">
          <span className={`knowledge-lifecycle knowledge-lifecycle--${page.lifecycle}`}>
            {humanize(page.lifecycle)}
          </span>
          <span className="status">{page.belief_strength} belief</span>
        </div>
      </div>
      <p className="knowledge-detail__statement">{page.statement}</p>
      {page.claim ? (
        <dl className="knowledge-claim">
          <div><dt>Dimension</dt><dd>{page.claim.dimension}</dd></div>
          <div><dt>Value</dt><dd>{page.claim.value}</dd></div>
          <div>
            <dt>Applies when</dt>
            <dd>{page.claim.conditions.length > 0 ? page.claim.conditions.join(", ") : "Always"}</dd>
          </div>
        </dl>
      ) : null}

      {page.lifecycle !== "retired" ? (
        <div className="feedback-actions">
          <button type="button" onClick={() => setMode("correct")} disabled={busy}>
            Correct this
          </button>
          <button
            type="button"
            className="button--danger"
            onClick={() => setMode("retire")}
            disabled={busy}
          >
            Retire belief
          </button>
        </div>
      ) : (
        <p className="knowledge-retired-note">
          This belief is retired, so the agent will not use it as current household knowledge.
        </p>
      )}

      {mode === "correct" ? (
        <form className="knowledge-edit-form" onSubmit={saveCorrection}>
          <label>
            Correct household knowledge
            <textarea
              value={statement}
              onChange={(event) => setStatement(event.target.value)}
              maxLength={2000}
              rows={5}
              required
            />
          </label>
          <p>The old wording will remain as an immutable revision with its provenance.</p>
          <div className="button-row button-row--compact">
            <button type="submit" disabled={busy}>{busy ? "Saving…" : "Save correction"}</button>
            <button type="button" className="button--quiet" onClick={cancel} disabled={busy}>Cancel</button>
          </div>
        </form>
      ) : null}

      {mode === "retire" ? (
        <form className="knowledge-edit-form" onSubmit={retire}>
          <label>
            Why is this no longer reliable? (optional)
            <textarea
              value={retirementReason}
              onChange={(event) => setRetirementReason(event.target.value)}
              maxLength={2000}
              rows={4}
            />
          </label>
          <p>Retiring prevents future use. It does not delete or hide the evidence trail.</p>
          <div className="button-row button-row--compact">
            <button type="submit" className="button--danger" disabled={busy}>
              {busy ? "Retiring…" : "Retire and preserve history"}
            </button>
            <button type="button" className="button--quiet" onClick={cancel} disabled={busy}>Cancel</button>
          </div>
        </form>
      ) : null}

      {message ? <p className="form-message" role="status">{message}</p> : null}

      <div className="knowledge-history">
        <h3>Revision history ({history.revisions.length})</h3>
        <div className="revision-list">
          {[...history.revisions].reverse().map((revision) => (
            <KnowledgeRevisionCard key={revision.id} revision={revision} />
          ))}
        </div>
      </div>
    </section>
  );
}

export default function KnowledgePage() {
  const [pages, setPages] = useState<KnowledgePageRecord[]>([]);
  const [selectedPageId, setSelectedPageId] = useState<string>();
  const [history, setHistory] = useState<KnowledgePageHistory>();
  const [includeRetired, setIncludeRetired] = useState(false);
  const [newStatement, setNewStatement] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("Loading household knowledge…");

  const loadPages = useCallback(async (preferredPageId?: string) => {
    const nextPages = await listKnowledge(includeRetired);
    setPages(nextPages);
    const nextSelectedId = preferredPageId && nextPages.some((page) => page.id === preferredPageId)
      ? preferredPageId
      : nextPages[0]?.id;
    setSelectedPageId(nextSelectedId);
    if (nextSelectedId) {
      setHistory(await getKnowledgePage(nextSelectedId));
    } else {
      setHistory(undefined);
    }
    setMessage("");
  }, [includeRetired]);

  useEffect(() => {
    void loadPages().catch((error: unknown) => {
      setMessage(error instanceof Error ? error.message : "Household knowledge is unavailable.");
    });
  }, [loadPages]);

  const selectPage = async (pageId: string) => {
    setSelectedPageId(pageId);
    setMessage("Loading revision history…");
    try {
      setHistory(await getKnowledgePage(pageId));
      setMessage("");
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Could not load that page.");
    }
  };

  const teach = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const statement = newStatement.trim();
    if (!statement) return;
    setBusy(true);
    setMessage("Adding your exact statement to the household wiki…");
    try {
      const result = await teachKnowledge(statement, crypto.randomUUID());
      setNewStatement("");
      await loadPages(result.page.id);
      setMessage("Added as confirmed household knowledge with a preserved source revision.");
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Could not add household knowledge.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="knowledge-page">
      <header className="knowledge-page__header">
        <div>
          <p className="eyebrow">What FoodLog has learned</p>
          <h1>Household wiki.</h1>
          <p>
            Inspect what the agent may rely on, where each belief came from, and every
            correction it has received.
          </p>
        </div>
        <div className="knowledge-page__account">
          <SessionControls />
          <Link to="/">Back to food journal</Link>
        </div>
      </header>

      <section className="knowledge-teach" aria-labelledby="teach-title">
        <div>
          <p className="section-kicker">Teach something stable</p>
          <h2 id="teach-title">What should FoodLog remember?</h2>
          <p>
            Use this for durable household facts—not a one-off plan such as tomorrow's duck.
          </p>
        </div>
        <form onSubmit={teach}>
          <label>
            Household knowledge
            <textarea
              value={newStatement}
              onChange={(event) => setNewStatement(event.target.value)}
              placeholder="Meat in the air-fryer basket by the sink is usually steak."
              maxLength={2000}
              rows={4}
              required
            />
          </label>
          <button type="submit" disabled={busy}>{busy ? "Saving…" : "Add to household wiki"}</button>
        </form>
      </section>

      <div className="knowledge-toolbar">
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={includeRetired}
            onChange={(event) => setIncludeRetired(event.target.checked)}
          />
          Show retired beliefs
        </label>
        <button
          type="button"
          className="button--quiet"
          onClick={() => loadPages(selectedPageId)}
          disabled={busy}
        >
          Refresh
        </button>
      </div>
      {message ? <p className="empty-state" role="status">{message}</p> : null}

      <div className="knowledge-workspace">
        <nav className="knowledge-index" aria-label="Household wiki pages">
          {pages.length === 0 && !message ? (
            <p className="empty-state">No {includeRetired ? "stored" : "active"} household beliefs yet.</p>
          ) : pages.map((page) => (
            <button
              key={page.id}
              type="button"
              className={`knowledge-index__item${selectedPageId === page.id ? " knowledge-index__item--selected" : ""}`}
              onClick={() => selectPage(page.id)}
            >
              <span>{page.title}</span>
              <small>{humanize(page.lifecycle)} · revision {page.current_revision_number}</small>
            </button>
          ))}
        </nav>
        {history ? <KnowledgeDetail key={history.page.current_revision_id} history={history} onChanged={loadPages} /> : null}
      </div>
    </main>
  );
}
