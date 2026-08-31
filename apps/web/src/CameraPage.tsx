import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  createBrowserCamera,
  provisionAccount,
  uploadCapture,
  type Account,
  type BrowserCamera,
} from "./api";
import { SessionControls, useAuth } from "./auth";
import {
  browserCameraDisplayName,
  browserCameraInstanceId,
  replaceBrowserCameraInstanceId,
} from "./browserCameraIdentity";
import { captureFrame } from "./cameraFrames";
import CameraSources from "./CameraSources";
import { useCaptureWakeLock } from "./useCaptureWakeLock";
import { MAX_MEMORY_QUEUE_DEPTH, useMotionCapture } from "./useMotionCapture";
import type { CaptureWakeLockStatus } from "./wakeLock";

function stopStream(stream: MediaStream | null): void {
  stream?.getTracks().forEach((track) => track.stop());
}

function wakeLockLabel(status: CaptureWakeLockStatus): string {
  switch (status) {
    case "active": return "active";
    case "requesting": return "requesting";
    case "hidden": return "inactive while hidden";
    case "unsupported": return "unsupported";
    case "denied": return "permission denied";
    case "released": return "released";
    default: return "inactive";
  }
}

export default function CameraPage() {
  const { user } = useAuth();
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const sequenceIdRef = useRef(`browser-${crypto.randomUUID()}`);
  const cameraInstanceIdRef = useRef(browserCameraInstanceId());
  const sequenceNumberRef = useRef(0);
  const [account, setAccount] = useState<Account>();
  const [camera, setCamera] = useState<BrowserCamera>();
  const [running, setRunning] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState(
    "Connecting this phone or browser automatically…",
  );
  const wakeLockStatus = useCaptureWakeLock(running);

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
    ownerUserId: user?.uid ?? "",
    camera,
    sequenceId: sequenceIdRef.current,
    takeSequenceNumber,
    onAccepted: updateAccountQuota,
    onMessage: setMessage,
  });
  const entitlementExhausted = account?.entitlement_mode === "trial"
    && account.trial_image_limit !== null
    && account.accepted_image_count >= account.trial_image_limit;
  const unattendedReady = running
    && motion.active
    && wakeLockStatus === "active"
    && !motion.blockedReason
    && !entitlementExhausted;

  const connectBrowserCamera = useCallback(async (): Promise<void> => {
    setBusy(true);
    setMessage("Connecting this browser securely…");
    try {
      const currentAccount = await provisionAccount();
      const cameraName = browserCameraDisplayName(cameraInstanceIdRef.current);
      let nextCamera = await createBrowserCamera(
        cameraName,
        cameraInstanceIdRef.current,
      );
      if (nextCamera.status === "revoked") {
        cameraInstanceIdRef.current = replaceBrowserCameraInstanceId();
        nextCamera = await createBrowserCamera(
          browserCameraDisplayName(cameraInstanceIdRef.current),
          cameraInstanceIdRef.current,
        );
      }
      setAccount(currentAccount);
      setCamera(nextCamera);
      setMessage("This browser is connected. Start the camera when you are ready.");
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Could not connect this browser.");
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void connectBrowserCamera();
    return () => stopStream(streamRef.current);
  }, [connectBrowserCamera, user?.uid]);

  const startCamera = async () => {
    if (entitlementExhausted) {
      setMessage("The image entitlement is exhausted. Camera capture cannot start.");
      return;
    }
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
      stream.getVideoTracks().forEach((track) => {
        track.addEventListener("ended", () => {
          motion.stop();
          streamRef.current = null;
          if (videoRef.current) videoRef.current.srcObject = null;
          setRunning(false);
          setMessage("The camera stream ended. No new frames are being captured.");
        }, { once: true });
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
        ? `Manual mode selected. ${motion.queueDepth} captured motion frames remain queued on this device.`
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

  const handleCurrentBrowserRevoked = () => {
    pauseCamera();
    setCamera(undefined);
    cameraInstanceIdRef.current = replaceBrowserCameraInstanceId();
    setMessage("This browser source was revoked. Reconnect it when you want to use it again.");
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

      <CameraSources
        account={account}
        currentBrowserCameraId={camera?.id}
        onCurrentBrowserRevoked={handleCurrentBrowserRevoked}
      />

      {camera ? (
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
      ) : null}

      <section className="manual-camera" aria-labelledby="manual-camera-title">
        <div className={`camera-frame ${running ? "camera-frame--active" : ""}`}>
          <video ref={videoRef} autoPlay muted playsInline aria-label="Live camera preview" />
          {!running ? <span>Camera paused</span> : null}
        </div>
        <div className="manual-camera__controls">
          <h2 id="manual-camera-title">Camera controls</h2>
          <p role="status">{message}</p>
          {camera ? (
            <dl className="motion-status" aria-label="Capture readiness">
              <div>
                <dt>Camera stream</dt>
                <dd>{running ? "running" : "paused"}</dd>
              </div>
              <div>
                <dt>Motion state</dt>
                <dd>{motion.active ? (motion.phase === "idle" ? "watching" : motion.phase) : "paused"}</dd>
              </div>
              <div>
                <dt>Delivery</dt>
                <dd>
                  {motion.blockedReason
                    ? "blocked"
                    : motion.delivering
                      ? "uploading"
                      : `${motion.queueDepth} queued`}
                </dd>
              </div>
              <div>
                <dt>Screen awake</dt>
                <dd>{wakeLockLabel(wakeLockStatus)}</dd>
              </div>
              <div>
                <dt>Entitlement</dt>
                <dd>
                  {account?.entitlement_mode === "unlimited"
                    ? "unlimited"
                    : account?.trial_image_limit == null
                      ? "unknown"
                      : `${Math.max(0, account.trial_image_limit - account.accepted_image_count)} remaining`}
                </dd>
              </div>
              <div>
                <dt>Unattended capture</dt>
                <dd>{unattendedReady ? "ready" : "not ready"}</dd>
              </div>
            </dl>
          ) : null}
          <div className="button-row">
            {!camera ? (
              <button type="button" onClick={() => void connectBrowserCamera()} disabled={busy}>
                {busy ? "Connecting…" : "Reconnect this browser"}
              </button>
            ) : null}
            {!running ? (
              <button
                type="button"
                onClick={startCamera}
                disabled={!camera || busy || entitlementExhausted}
              >
                Start camera
              </button>
            ) : (
              <button type="button" className="button--quiet" onClick={pauseCamera} disabled={busy}>
                Pause camera
              </button>
            )}
            {running && !motion.active ? (
              <button
                type="button"
                className="button--quiet"
                onClick={startMotionMode}
                disabled={busy || motion.blockedReason !== undefined}
              >
                Start motion mode
              </button>
            ) : null}
            {running && motion.active ? (
              <button type="button" className="button--quiet" onClick={useManualMode}>
                Use manual mode
              </button>
            ) : null}
            <button
              type="button"
              onClick={sendSnapshot}
              disabled={!running || busy || motion.active || entitlementExhausted}
            >
              {busy && running ? "Sending…" : "Send snapshot"}
            </button>
            {motion.queueDepth > 0 && !motion.delivering && !motion.blockedReason ? (
              <button type="button" className="button--quiet" onClick={retryQueuedUploads}>
                Retry queued uploads
              </button>
            ) : null}
          </div>
          <p className="fine-print">
            Manual mode uploads only when you press Send snapshot. Motion mode compares
            tiny frames on this device, captures at most once per second during a
            15-second burst, then once per minute while activity remains open. Up to{" "}
            {MAX_MEMORY_QUEUE_DEPTH} pending frames persist on this device across
            temporary outages and page reloads, then retry oldest-first. The page asks
            to keep the screen awake only while the camera stream is running; if that
            lock is unavailable, Unattended capture remains Not ready.
          </p>
        </div>
      </section>
    </main>
  );
}
