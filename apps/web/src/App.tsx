import { useCallback, useEffect, useRef, useState } from "react";
import {
  answerQuestion,
  createBrowserCamera,
  listJournal,
  listMealRevisions,
  listOpenQuestions,
  loadCaptureImage,
  provisionAccount,
  submitMealFeedback,
  uploadCapture,
  type Account,
  type BrowserCamera,
  type ClarificationQuestion,
  type MealEntry,
  type MealRevision,
  type MealStatus,
} from "./api";
import { SessionControls } from "./auth";

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
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const [account, setAccount] = useState<Account>();
  const [camera, setCamera] = useState<BrowserCamera>();
  const [journal, setJournal] = useState<MealEntry[]>([]);
  const [questions, setQuestions] = useState<ClarificationQuestion[]>([]);
  const [capturing, setCapturing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("Preparing your private local trial…");

  const refreshWorkspace = useCallback(async () => {
    const [entries, currentAccount, openQuestions] = await Promise.all([
      listJournal(),
      provisionAccount(),
      listOpenQuestions(),
    ]);
    setJournal(entries);
    setAccount(currentAccount);
    setQuestions(openQuestions);
  }, []);

  useEffect(() => {
    Promise.all([provisionAccount(), createBrowserCamera("Browser trial camera")])
      .then(([nextAccount, nextCamera]) => {
        setAccount(nextAccount);
        setCamera(nextCamera);
        setMessage("Ready. Start the camera when it points at the cooking area.");
        return refreshWorkspace();
      })
      .catch((error: unknown) => {
        setMessage(error instanceof Error ? error.message : "Local API unavailable");
      });

    return () => {
      streamRef.current?.getTracks().forEach((track) => track.stop());
    };
  }, [refreshWorkspace]);

  const startCamera = async () => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: "environment" },
        audio: false,
      });
      streamRef.current = stream;
      if (videoRef.current) videoRef.current.srcObject = stream;
      setCapturing(true);
      setMessage("Camera active. Keep this tab open for the browser trial.");
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Camera permission failed");
    }
  };

  const stopCamera = () => {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    if (videoRef.current) videoRef.current.srcObject = null;
    setCapturing(false);
    setMessage("Camera paused.");
  };

  const analyzeCurrentFrame = async () => {
    if (!camera || !videoRef.current) return;
    setBusy(true);
    setMessage("Saving and analysing this frame…");
    try {
      const video = videoRef.current;
      const canvas = document.createElement("canvas");
      canvas.width = video.videoWidth;
      canvas.height = video.videoHeight;
      const context = canvas.getContext("2d");
      if (!context || canvas.width === 0 || canvas.height === 0) {
        throw new Error("The camera has not produced a frame yet.");
      }
      context.drawImage(video, 0, 0, canvas.width, canvas.height);
      const image = await new Promise<Blob>((resolve, reject) => {
        canvas.toBlob(
          (blob) => (blob ? resolve(blob) : reject(new Error("Capture failed"))),
          "image/jpeg",
          0.86,
        );
      });
      await uploadCapture(camera.id, image, crypto.randomUUID());
      await refreshWorkspace();
      setMessage("Journal updated. Answer any useful clarification below.");
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Capture failed");
    } finally {
      setBusy(false);
    }
  };

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
          <strong>{account?.accepted_image_count ?? 0} / {account?.trial_image_limit ?? 200} images</strong>
        </div>
      </header>

      <section className="capture-panel" aria-labelledby="capture-title">
        <div>
          <p className="section-kicker">Browser trial camera</p>
          <h2 id="capture-title">Point. Start. Carry on cooking.</h2>
          <p>{message}</p>
          <div className="button-row">
            {!capturing ? (
              <button type="button" onClick={startCamera}>Start camera</button>
            ) : (
              <button type="button" className="button--quiet" onClick={stopCamera}>Pause camera</button>
            )}
            <button
              type="button"
              onClick={analyzeCurrentFrame}
              disabled={!capturing || busy}
            >
              {busy ? "Analysing…" : "Analyze current frame"}
            </button>
          </div>
          <p className="fine-print">
            This zero-install trial needs the tab open and the computer awake.
            The physical camera is the unattended product path.
          </p>
        </div>
        <div className={`camera-frame ${capturing ? "camera-frame--active" : ""}`}>
          <video ref={videoRef} autoPlay muted playsInline />
          {!capturing ? <span>Camera paused</span> : null}
        </div>
      </section>

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
