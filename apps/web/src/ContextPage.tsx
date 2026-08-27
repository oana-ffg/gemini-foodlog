import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import {
  createContextNote,
  listContextNotes,
  retireContextNote,
  teachKnowledge,
  type UserContextNote,
  type UserContextNoteInput,
} from "./api";
import { SessionControls } from "./auth";

type WindowPreset = "tomorrow" | "next_day" | "next_week" | "until_removed" | "custom";

interface OperationIdentity {
  payload: string;
  key: string;
}

function idempotencyKeyFor(
  identity: OperationIdentity | undefined,
  payload: string,
): OperationIdentity {
  return identity?.payload === payload
    ? identity
    : { payload, key: crypto.randomUUID() };
}

function tomorrowWindow(now = new Date()): Pick<UserContextNoteInput, "valid_from" | "valid_until"> {
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 1);
  const end = new Date(now.getFullYear(), now.getMonth(), now.getDate() + 2);
  return { valid_from: start.toISOString(), valid_until: end.toISOString() };
}

function contextWindow(
  preset: WindowPreset,
  customStart: string,
  customEnd: string,
  now = new Date(),
): Pick<UserContextNoteInput, "valid_from" | "valid_until"> {
  if (preset === "until_removed") return {};
  if (preset === "tomorrow") return tomorrowWindow(now);
  if (preset === "next_day" || preset === "next_week") {
    return {
      valid_from: now.toISOString(),
      valid_until: new Date(
        now.getTime() + (preset === "next_day" ? 24 : 7 * 24) * 60 * 60 * 1000,
      ).toISOString(),
    };
  }
  if (!customStart || !customEnd) {
    throw new Error("Choose both a start and an end for the custom window.");
  }
  const start = new Date(customStart);
  const end = new Date(customEnd);
  if (Number.isNaN(start.valueOf()) || Number.isNaN(end.valueOf()) || end <= start) {
    throw new Error("The custom end must be after the start.");
  }
  return { valid_from: start.toISOString(), valid_until: end.toISOString() };
}

function toLocalInput(value: string | null): string {
  if (!value) return "";
  const date = new Date(value);
  const offset = date.getTimezoneOffset() * 60_000;
  return new Date(date.getTime() - offset).toISOString().slice(0, 16);
}

function notePhase(note: UserContextNote, now = new Date()): "active" | "scheduled" | "expired" | "retired" {
  if (note.status === "retired") return "retired";
  if (note.valid_from && new Date(note.valid_from) > now) return "scheduled";
  if (note.valid_until && new Date(note.valid_until) <= now) return "expired";
  return "active";
}

function noteWindowLabel(note: UserContextNote): string {
  if (!note.valid_from && !note.valid_until) return "Until you retire it";
  if (note.valid_from && note.valid_until) {
    return `${new Date(note.valid_from).toLocaleString()} – ${new Date(note.valid_until).toLocaleString()}`;
  }
  if (note.valid_from) return `From ${new Date(note.valid_from).toLocaleString()}`;
  return `Until ${new Date(note.valid_until!).toLocaleString()}`;
}

interface ContextNoteCardProps {
  note: UserContextNote;
  busy: boolean;
  onEdit: (note: UserContextNote) => void;
  onRetire: (note: UserContextNote) => Promise<void>;
  onPromote: (note: UserContextNote) => Promise<void>;
}

export function ContextNoteCard({
  note,
  busy,
  onEdit,
  onRetire,
  onPromote,
}: ContextNoteCardProps) {
  const [confirming, setConfirming] = useState<"retire" | "promote">();
  const phase = notePhase(note);
  const current = phase === "active" || phase === "scheduled";
  return (
    <article className="context-note">
      <div className="entry-meta">
        <span className={`context-phase context-phase--${phase}`}>{phase}</span>
        <time dateTime={note.created_at}>{new Date(note.created_at).toLocaleString()}</time>
      </div>
      <p className="context-note__text">{note.text}</p>
      <dl className="context-note__meta">
        <div><dt>Agent-use window</dt><dd>{noteWindowLabel(note)}</dd></div>
        <div><dt>Stored as</dt><dd>Exact user wording</dd></div>
      </dl>
      {current ? (
        <div className="feedback-actions">
          <button type="button" onClick={() => onEdit(note)} disabled={busy}>Edit with history</button>
          <button type="button" className="button--quiet" onClick={() => setConfirming("promote")} disabled={busy}>
            Make permanent knowledge
          </button>
          <button type="button" className="button--danger" onClick={() => setConfirming("retire")} disabled={busy}>
            Retire note
          </button>
        </div>
      ) : null}
      {confirming === "promote" ? (
        <div className="context-confirmation">
          <p>
            This copies the exact statement into permanent household knowledge, then retires
            the temporary note. Both source records stay auditable.
          </p>
          <div className="button-row button-row--compact">
            <button type="button" onClick={() => onPromote(note)} disabled={busy}>Yes, make permanent</button>
            <button type="button" className="button--quiet" onClick={() => setConfirming(undefined)} disabled={busy}>Cancel</button>
          </div>
        </div>
      ) : null}
      {confirming === "retire" ? (
        <div className="context-confirmation">
          <p>The agent will stop using this note. Its exact wording remains in history.</p>
          <div className="button-row button-row--compact">
            <button type="button" className="button--danger" onClick={() => onRetire(note)} disabled={busy}>Yes, retire note</button>
            <button type="button" className="button--quiet" onClick={() => setConfirming(undefined)} disabled={busy}>Cancel</button>
          </div>
        </div>
      ) : null}
    </article>
  );
}

export default function ContextPage() {
  const createIdentity = useRef<OperationIdentity | undefined>(undefined);
  const editIdentities = useRef(new Map<string, OperationIdentity>());
  const promotionIdentities = useRef(new Map<string, OperationIdentity>());
  const [notes, setNotes] = useState<UserContextNote[]>([]);
  const [text, setText] = useState("");
  const [preset, setPreset] = useState<WindowPreset>("tomorrow");
  const [customStart, setCustomStart] = useState("");
  const [customEnd, setCustomEnd] = useState("");
  const [editing, setEditing] = useState<UserContextNote>();
  const [showHistory, setShowHistory] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("Loading your context notes…");

  const refresh = useCallback(async () => {
    setNotes(await listContextNotes(true));
    setMessage("");
  }, []);

  useEffect(() => {
    void refresh().catch((error: unknown) => {
      setMessage(error instanceof Error ? error.message : "Context notes are unavailable.");
    });
  }, [refresh]);

  const resetComposer = () => {
    setText("");
    setPreset("tomorrow");
    setCustomStart("");
    setCustomEnd("");
    setEditing(undefined);
  };

  const noteInput = (): UserContextNoteInput => {
    const trimmed = text.trim();
    if (!trimmed) throw new Error("Tell FoodLog what context it should consider.");
    return { text: trimmed, ...contextWindow(preset, customStart, customEnd) };
  };

  const save = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    let input: UserContextNoteInput;
    try {
      input = noteInput();
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "The context window is invalid.");
      return;
    }
    const payload = JSON.stringify(input);
    setBusy(true);
    setMessage(editing ? "Saving the replacement before retiring the old note…" : "Saving exact context…");
    try {
      if (editing) {
        const identity = idempotencyKeyFor(editIdentities.current.get(editing.id), payload);
        editIdentities.current.set(editing.id, identity);
        await createContextNote(input, identity.key);
        await retireContextNote(editing.id);
        editIdentities.current.delete(editing.id);
      } else {
        const identity = idempotencyKeyFor(createIdentity.current, payload);
        createIdentity.current = identity;
        await createContextNote(input, identity.key);
        createIdentity.current = undefined;
      }
      resetComposer();
      await refresh();
      setMessage(
        editing
          ? "Updated. The old note is retired and preserved; the replacement is active."
          : "Context saved. FoodLog will consider it only inside the displayed window.",
      );
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Could not save this context.");
    } finally {
      setBusy(false);
    }
  };

  const startEdit = (note: UserContextNote) => {
    setEditing(note);
    setText(note.text);
    setPreset(note.valid_from || note.valid_until ? "custom" : "until_removed");
    setCustomStart(toLocalInput(note.valid_from));
    setCustomEnd(toLocalInput(note.valid_until));
    setMessage("Editing creates a replacement and retires this version; history stays intact.");
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const retire = async (note: UserContextNote) => {
    setBusy(true);
    setMessage("Retiring the note…");
    try {
      await retireContextNote(note.id);
      await refresh();
      setMessage("Retired. The exact note remains visible in history and is no longer agent context.");
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Could not retire this note.");
    } finally {
      setBusy(false);
    }
  };

  const promote = async (note: UserContextNote) => {
    const payload = note.text;
    const identity = idempotencyKeyFor(promotionIdentities.current.get(note.id), payload);
    promotionIdentities.current.set(note.id, identity);
    setBusy(true);
    setMessage("Creating durable household knowledge before retiring the temporary note…");
    try {
      await teachKnowledge(note.text, identity.key);
      await retireContextNote(note.id);
      promotionIdentities.current.delete(note.id);
      await refresh();
      setMessage("Converted. The exact statement is now in the household wiki; this temporary note is retired.");
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Could not convert this note.");
    } finally {
      setBusy(false);
    }
  };

  const currentNotes = notes.filter((note) => {
    const phase = notePhase(note);
    return phase === "active" || phase === "scheduled";
  });
  const historicalNotes = notes.filter((note) => {
    const phase = notePhase(note);
    return phase === "expired" || phase === "retired";
  });

  return (
    <main className="context-page">
      <header className="context-page__header">
        <div>
          <p className="eyebrow">User-initiated context</p>
          <h1>Tell FoodLog.</h1>
          <p>
            Add useful context whenever you have it. This is independent of meal corrections
            and agent questions.
          </p>
        </div>
        <div className="context-page__account">
          <SessionControls />
          <Link to="/">Back to food journal</Link>
          <Link to="/knowledge">Open household wiki</Link>
        </div>
      </header>

      <section className="context-composer" aria-labelledby="context-composer-title">
        <div>
          <p className="section-kicker">{editing ? "Replace a note" : "Proactive context"}</p>
          <h2 id="context-composer-title">
            {editing ? "What should the new version say?" : "What should the agent know?"}
          </h2>
          <p>
            A temporary note influences inference only during its chosen window. It does not
            silently become a permanent preference.
          </p>
        </div>
        <form onSubmit={save}>
          <label>
            Exact context
            <textarea
              value={text}
              onChange={(event) => setText(event.target.value)}
              placeholder="My MIL brought duck, and we intend to cook it tomorrow."
              maxLength={4000}
              rows={5}
              required
            />
          </label>
          <label>
            Use this context
            <select value={preset} onChange={(event) => setPreset(event.target.value as WindowPreset)}>
              <option value="tomorrow">Tomorrow</option>
              <option value="next_day">For the next 24 hours</option>
              <option value="next_week">For the next 7 days</option>
              <option value="until_removed">Until I remove it</option>
              <option value="custom">Custom dates</option>
            </select>
          </label>
          {preset === "custom" ? (
            <div className="context-custom-window">
              <label>
                Starts
                <input
                  type="datetime-local"
                  value={customStart}
                  onChange={(event) => setCustomStart(event.target.value)}
                  required
                />
              </label>
              <label>
                Ends
                <input
                  type="datetime-local"
                  value={customEnd}
                  onChange={(event) => setCustomEnd(event.target.value)}
                  required
                />
              </label>
            </div>
          ) : null}
          <div className="button-row button-row--compact">
            <button type="submit" disabled={busy}>{busy ? "Saving…" : editing ? "Save replacement" : "Save context"}</button>
            {editing ? (
              <button type="button" className="button--quiet" onClick={resetComposer} disabled={busy}>Cancel edit</button>
            ) : null}
          </div>
        </form>
      </section>

      {message ? <p className="empty-state context-message" role="status">{message}</p> : null}

      <section className="context-list" aria-labelledby="current-context-title">
        <div className="section-heading">
          <div>
            <p className="section-kicker">Available to the agent</p>
            <h2 id="current-context-title">Current and scheduled context</h2>
          </div>
          <span className="question-count">{currentNotes.length} notes</span>
        </div>
        {currentNotes.length === 0 ? (
          <p className="empty-state">No temporary context is active or scheduled.</p>
        ) : currentNotes.map((note) => (
          <ContextNoteCard
            key={note.id}
            note={note}
            busy={busy}
            onEdit={startEdit}
            onRetire={retire}
            onPromote={promote}
          />
        ))}
      </section>

      <section className="context-history" aria-labelledby="context-history-title">
        <button type="button" className="text-button" onClick={() => setShowHistory((current) => !current)}>
          {showHistory ? "Hide" : "View"} expired and retired note history ({historicalNotes.length})
        </button>
        {showHistory ? (
          <div className="context-history__list">
            <h2 id="context-history-title">Preserved note history</h2>
            {historicalNotes.length === 0 ? (
              <p className="empty-state">No expired or retired notes yet.</p>
            ) : historicalNotes.map((note) => (
              <ContextNoteCard
                key={note.id}
                note={note}
                busy={busy}
                onEdit={startEdit}
                onRetire={retire}
                onPromote={promote}
              />
            ))}
          </div>
        ) : null}
      </section>
    </main>
  );
}
