import { type FormEvent, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  createBrowserCamera,
  provisionAccount,
  uploadCapture,
  type Account,
  type BrowserCamera,
} from "./api";
import { SessionControls } from "./auth";
import { captureFrame } from "./cameraFrames";
import { MAX_MEMORY_QUEUE_DEPTH, useMotionCapture } from "./useMotionCapture";

function stopStream(stream: MediaStream | null): void {
  stream?.getTracks().forEach((track) => track.stop());
}

export default function CameraPage() {
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const sequenceIdRef = useRef(`browser-${crypto.randomUUID()}`);
  const sequenceNumberRef = useRef(0);
  const [account, setAccount] = useState<Account>();
  const [camera, setCamera] = useState<BrowserCamera>();
  const [cameraName, setCameraName] = useState("");
  const [running, setRunning] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(
    "Register this phone or browser before starting its camera.",
  );

  const updateAccountQuota = (accepted: Awaited<ReturnType<typeof uploadCapture>>) => {
    setAccount((current) => current ? {
      ...current,
      accepted_image_count: accepted.accepted_image_count,
      entitlement_mode: accepted.entitlement_mode,
      trial_image_limit: accepted.trial_image_limit,
    } : current);
  };

  const takeSequenceNumber = () => {
    const sequenceNumber = sequenceNumberRef.current;
    sequenceNumberRef.current += 1;
    return sequenceNumber;
  };

  const motion = useMotionCapture({
    videoRef,
    camera,
    sequenceId: sequenceIdRef.current,
    takeSequenceNumber,
    onAccepted: updateAccountQuota,
    onMessage: setMessage,
  });

  useEffect(() => () => stopStream(streamRef.current), []);

  const registerCamera = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const name = cameraName.trim();
    if (!name) return;

    setBusy(true);
    setMessage("Registering this camera…");
    try {
      const currentAccount = await provisionAccount();
      const nextCamera = await createBrowserCamera(name);
      setAccount(currentAccount);
      setCamera(nextCamera);
      setMessage("Camera registered. Start it when it points at the cooking area.");
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Camera registration failed.");
    } finally {
      setBusy(false);
    }
  };

  const startCamera = async () => {
    if (!navigator.mediaDevices?.getUserMedia) {
      setMessage("This browser does not provide camera access.");
      return;
    }

    setBusy(true);
    setMessage("Requesting camera access…");
    try {
      stopStream(streamRef.current);
      const stream = await navigator.mediaDevices.getUserMedia({
        video: {
          facingMode: { ideal: "environment" },
          width: { ideal: 1920, max: 4096 },
          height: { ideal: 1080, max: 4096 },
        },
        audio: false,
      });
      streamRef.current = stream;
      if (videoRef.current) videoRef.current.srcObject = stream;
      setRunning(true);
      setMessage("Camera is running. Choose manual snapshot or motion mode.");
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Camera permission failed.");
    } finally {
      setBusy(false);
    }
  };

  const startMotionMode = () => {
    if (!camera || !running || !videoRef.current || motion.active) return;
    motion.start();
  };

  const useManualMode = () => {
    motion.stop();
    setMessage(
      motion.queueDepth > 0
        ? `Manual mode selected. ${motion.queueDepth} captured motion frames are still queued in this tab.`
        : "Manual mode selected. Nothing is uploaded until you press Send snapshot.",
    );
  };

  const pauseCamera = () => {
    motion.stop();
    stopStream(streamRef.current);
    streamRef.current = null;
    if (videoRef.current) videoRef.current.srcObject = null;
    setRunning(false);
    setMessage(
      motion.queueDepth > 0
        ? `Camera paused. ${motion.queueDepth} already captured frames remain queued for delivery.`
        : "Camera paused. No images are being captured or uploaded.",
    );
  };

  const sendSnapshot = async () => {
    if (!camera || !videoRef.current) return;

    setBusy(true);
    setMessage("Uploading this snapshot securely…");
    try {
      const frame = await captureFrame(videoRef.current);
      const sequenceNumber = takeSequenceNumber();
      const accepted = await uploadCapture(
        camera.id,
        frame.image,
        crypto.randomUUID(),
        {
          capturedAt: new Date().toISOString(),
          sequenceId: sequenceIdRef.current,
          sequenceNumber,
          width: frame.width,
          height: frame.height,
        },
      );
      updateAccountQuota(accepted);
      setMessage(
        accepted.duplicate
          ? "That snapshot was already stored; quota was not charged twice."
          : "Snapshot stored. No AI analysis was triggered by this upload.",
      );
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Snapshot upload failed.");
    } finally {
      setBusy(false);
    }
  };

  const retryQueuedUploads = () => {
    motion.retry();
  };

  return (
    <main className="camera-page">
      <header className="camera-page__header">
        <div>
          <p className="eyebrow">Browser kitchen camera</p>
          <h1>Watch the kitchen without performing for it.</h1>
          <p>Send one manual snapshot, or let local motion detection capture an activity burst.</p>
        </div>
        <div className="camera-page__account">
          <SessionControls />
          <Link to="/">Back to journal</Link>
        </div>
      </header>

      {!camera ? (
        <form className="camera-registration" onSubmit={registerCamera}>
          <label>
            Name this camera
            <input
              value={cameraName}
              onChange={(event) => setCameraName(event.target.value)}
              maxLength={80}
              required
              autoComplete="off"
            />
          </label>
          <button type="submit" disabled={busy || cameraName.trim().length === 0}>
            {busy ? "Registering…" : "Register camera"}
          </button>
        </form>
      ) : (
        <p className="camera-page__identity">
          Sending as <strong>{camera.name}</strong>
          {account ? (
            <span>
              {account.accepted_image_count} / {account.entitlement_mode === "unlimited"
                ? "Unlimited"
                : account.trial_image_limit} images used
            </span>
          ) : null}
        </p>
      )}

      <section className="manual-camera" aria-labelledby="manual-camera-title">
        <div className={`camera-frame ${running ? "camera-frame--active" : ""}`}>
          <video ref={videoRef} autoPlay muted playsInline aria-label="Live camera preview" />
          {!running ? <span>Camera paused</span> : null}
        </div>
        <div className="manual-camera__controls">
          <h2 id="manual-camera-title">Camera controls</h2>
          <p role="status">{message}</p>
          {motion.active || motion.queueDepth > 0 ? (
            <dl className="motion-status">
              <div>
                <dt>Motion state</dt>
                <dd>{motion.active ? (motion.phase === "idle" ? "watching" : motion.phase) : "paused"}</dd>
              </div>
              <div>
                <dt>Delivery</dt>
                <dd>{motion.delivering ? "uploading" : `${motion.queueDepth} queued`}</dd>
              </div>
            </dl>
          ) : null}
          <div className="button-row">
            {!running ? (
              <button type="button" onClick={startCamera} disabled={!camera || busy}>
                Start camera
              </button>
            ) : (
              <button type="button" className="button--quiet" onClick={pauseCamera} disabled={busy}>
                Pause camera
              </button>
            )}
            {running && !motion.active ? (
              <button type="button" className="button--quiet" onClick={startMotionMode} disabled={busy}>
                Start motion mode
              </button>
            ) : null}
            {running && motion.active ? (
              <button type="button" className="button--quiet" onClick={useManualMode}>
                Use manual mode
              </button>
            ) : null}
            <button type="button" onClick={sendSnapshot} disabled={!running || busy || motion.active}>
              {busy && running ? "Sending…" : "Send snapshot"}
            </button>
            {motion.queueDepth > 0 && !motion.delivering ? (
              <button type="button" className="button--quiet" onClick={retryQueuedUploads}>
                Retry queued uploads
              </button>
            ) : null}
          </div>
          <p className="fine-print">
            Manual mode uploads only when you press Send snapshot. Motion mode compares
            tiny frames on this device, captures at most once per second during a
            15-second burst, then once per minute while activity remains open. This
            version keeps up to {MAX_MEMORY_QUEUE_DEPTH} pending frames in this tab;
            persistent offline delivery comes next.
          </p>
        </div>
      </section>
    </main>
  );
}
