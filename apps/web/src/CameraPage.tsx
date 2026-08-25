import { type FormEvent, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  createBrowserCamera,
  provisionAccount,
  uploadCapture,
  type Account,
  type BrowserCaptureMetadata,
  type BrowserCamera,
} from "./api";
import { SessionControls } from "./auth";
import {
  advanceMotionCadence,
  analyseMotion,
  initialMotionCadenceState,
  type MotionCaptureDecision,
  type MotionPhase,
} from "./motion";

const MAX_CAPTURE_EDGE = 1920;
const MOTION_SAMPLE_WIDTH = 64;
const MOTION_SAMPLE_HEIGHT = 48;
const MOTION_SAMPLE_INTERVAL_MS = 250;
const MAX_MEMORY_QUEUE_DEPTH = 30;

interface QueuedCapture {
  image: Blob;
  idempotencyKey: string;
  metadata: BrowserCaptureMetadata;
}

function stopStream(stream: MediaStream | null): void {
  stream?.getTracks().forEach((track) => track.stop());
}

function outputDimensions(width: number, height: number): { width: number; height: number } {
  const scale = Math.min(1, MAX_CAPTURE_EDGE / Math.max(width, height));
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  };
}

async function captureFrame(video: HTMLVideoElement): Promise<{
  image: Blob;
  width: number;
  height: number;
}> {
  if (video.videoWidth === 0 || video.videoHeight === 0) {
    throw new Error("The camera has not produced a frame yet.");
  }

  const { width, height } = outputDimensions(video.videoWidth, video.videoHeight);
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const context = canvas.getContext("2d");
  if (!context) throw new Error("This browser cannot prepare a camera image.");
  context.drawImage(video, 0, 0, width, height);

  const image = await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob(
      (blob) => blob ? resolve(blob) : reject(new Error("The snapshot could not be encoded.")),
      "image/jpeg",
      0.82,
    );
  });
  return { image, width, height };
}

function sampleMotionFrame(
  video: HTMLVideoElement,
  canvas: HTMLCanvasElement,
): Uint8ClampedArray {
  canvas.width = MOTION_SAMPLE_WIDTH;
  canvas.height = MOTION_SAMPLE_HEIGHT;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (!context) throw new Error("This browser cannot inspect camera motion.");
  context.drawImage(video, 0, 0, MOTION_SAMPLE_WIDTH, MOTION_SAMPLE_HEIGHT);
  return new Uint8ClampedArray(
    context.getImageData(0, 0, MOTION_SAMPLE_WIDTH, MOTION_SAMPLE_HEIGHT).data,
  );
}

export default function CameraPage() {
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const sequenceIdRef = useRef(`browser-${crypto.randomUUID()}`);
  const sequenceNumberRef = useRef(0);
  const mountedRef = useRef(true);
  const motionModeRef = useRef(false);
  const motionTimerRef = useRef<number | null>(null);
  const motionSampleBusyRef = useRef(false);
  const motionCanvasRef = useRef<HTMLCanvasElement | null>(null);
  const previousMotionFrameRef = useRef<Uint8ClampedArray | null>(null);
  const motionCadenceRef = useRef(initialMotionCadenceState());
  const deliveryActiveRef = useRef(false);
  const captureQueueRef = useRef<QueuedCapture[]>([]);
  const [account, setAccount] = useState<Account>();
  const [camera, setCamera] = useState<BrowserCamera>();
  const [cameraName, setCameraName] = useState("");
  const [running, setRunning] = useState(false);
  const [busy, setBusy] = useState(false);
  const [motionMode, setMotionMode] = useState(false);
  const [motionPhase, setMotionPhase] = useState<MotionPhase>("idle");
  const [queueDepth, setQueueDepth] = useState(0);
  const [deliveryActive, setDeliveryActive] = useState(false);
  const [message, setMessage] = useState(
    "Register this phone or browser before starting its camera.",
  );

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (motionTimerRef.current !== null) window.clearInterval(motionTimerRef.current);
      stopStream(streamRef.current);
    };
  }, []);

  const stopMotionSampling = () => {
    if (motionTimerRef.current !== null) {
      window.clearInterval(motionTimerRef.current);
      motionTimerRef.current = null;
    }
    motionModeRef.current = false;
    previousMotionFrameRef.current = null;
    motionCadenceRef.current = initialMotionCadenceState();
    if (mountedRef.current) {
      setMotionMode(false);
      setMotionPhase("idle");
    }
  };

  const updateQueueDepth = () => {
    if (mountedRef.current) setQueueDepth(captureQueueRef.current.length);
  };

  const updateAccountQuota = (accepted: Awaited<ReturnType<typeof uploadCapture>>) => {
    if (!mountedRef.current) return;
    setAccount((current) => current ? {
      ...current,
      accepted_image_count: accepted.accepted_image_count,
      entitlement_mode: accepted.entitlement_mode,
      trial_image_limit: accepted.trial_image_limit,
    } : current);
  };

  const drainCaptureQueue = async () => {
    if (deliveryActiveRef.current) return;
    deliveryActiveRef.current = true;
    if (mountedRef.current) setDeliveryActive(true);

    while (captureQueueRef.current.length > 0) {
      const queued = captureQueueRef.current[0];
      if (!camera) break;
      try {
        const accepted = await uploadCapture(
          camera.id,
          queued.image,
          queued.idempotencyKey,
          queued.metadata,
        );
        captureQueueRef.current.shift();
        updateQueueDepth();
        updateAccountQuota(accepted);
        if (mountedRef.current) {
          setMessage(
            `Motion frame stored. ${captureQueueRef.current.length} queued for delivery.`,
          );
        }
      } catch (error: unknown) {
        stopMotionSampling();
        if (mountedRef.current) {
          setMessage(
            `Motion capture paused because delivery failed: ${
              error instanceof Error ? error.message : "unknown upload error"
            }. The captured frames remain queued in this tab.`,
          );
        }
        break;
      }
    }

    deliveryActiveRef.current = false;
    if (mountedRef.current) setDeliveryActive(false);
  };

  const enqueueMotionCapture = async (decision: MotionCaptureDecision) => {
    if (!videoRef.current) return;
    if (captureQueueRef.current.length >= MAX_MEMORY_QUEUE_DEPTH) {
      stopMotionSampling();
      setMessage(
        `Motion capture paused because the temporary queue reached ${MAX_MEMORY_QUEUE_DEPTH} frames. Keep this tab open and retry delivery.`,
      );
      return;
    }

    const frame = await captureFrame(videoRef.current);
    const sequenceNumber = sequenceNumberRef.current;
    sequenceNumberRef.current += 1;
    captureQueueRef.current.push({
      image: frame.image,
      idempotencyKey: crypto.randomUUID(),
      metadata: {
        capturedAt: new Date().toISOString(),
        sequenceId: sequenceIdRef.current,
        sequenceNumber,
        width: frame.width,
        height: frame.height,
        burstId: decision.burstId,
        burstFrameIndex: decision.burstFrameIndex,
        motion: decision.motion,
      },
    });
    updateQueueDepth();
    void drainCaptureQueue();
  };

  const sampleForMotion = async () => {
    const video = videoRef.current;
    if (
      !motionModeRef.current
      || motionSampleBusyRef.current
      || !video
      || video.videoWidth === 0
      || video.videoHeight === 0
    ) return;

    motionSampleBusyRef.current = true;
    try {
      motionCanvasRef.current ??= document.createElement("canvas");
      const currentFrame = sampleMotionFrame(video, motionCanvasRef.current);
      const previousFrame = previousMotionFrameRef.current;
      previousMotionFrameRef.current = currentFrame;
      if (!previousFrame) {
        setMessage("Motion mode is watching locally. Nothing has been uploaded yet.");
        return;
      }

      const analysis = analyseMotion(previousFrame, currentFrame);
      const result = advanceMotionCadence(
        motionCadenceRef.current,
        Date.now(),
        analysis,
        () => `motion-${crypto.randomUUID()}`,
      );
      motionCadenceRef.current = result.state;
      setMotionPhase(result.state.phase);
      if (result.capture) await enqueueMotionCapture(result.capture);
    } catch (error: unknown) {
      stopMotionSampling();
      setMessage(
        error instanceof Error ? error.message : "Motion detection stopped unexpectedly.",
      );
    } finally {
      motionSampleBusyRef.current = false;
    }
  };

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
    if (!camera || !running || !videoRef.current || motionModeRef.current) return;
    previousMotionFrameRef.current = null;
    motionCadenceRef.current = initialMotionCadenceState();
    motionModeRef.current = true;
    setMotionMode(true);
    setMotionPhase("idle");
    setMessage("Starting local motion detection…");
    void sampleForMotion();
    motionTimerRef.current = window.setInterval(
      () => void sampleForMotion(),
      MOTION_SAMPLE_INTERVAL_MS,
    );
  };

  const useManualMode = () => {
    stopMotionSampling();
    setMessage(
      captureQueueRef.current.length > 0
        ? `Manual mode selected. ${captureQueueRef.current.length} captured motion frames are still queued in this tab.`
        : "Manual mode selected. Nothing is uploaded until you press Send snapshot.",
    );
  };

  const pauseCamera = () => {
    stopMotionSampling();
    stopStream(streamRef.current);
    streamRef.current = null;
    if (videoRef.current) videoRef.current.srcObject = null;
    setRunning(false);
    setMessage(
      captureQueueRef.current.length > 0
        ? `Camera paused. ${captureQueueRef.current.length} already captured frames remain queued for delivery.`
        : "Camera paused. No images are being captured or uploaded.",
    );
  };

  const sendSnapshot = async () => {
    if (!camera || !videoRef.current) return;

    setBusy(true);
    setMessage("Uploading this snapshot securely…");
    try {
      const frame = await captureFrame(videoRef.current);
      const sequenceNumber = sequenceNumberRef.current;
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
      sequenceNumberRef.current += 1;
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
    setMessage("Retrying queued motion frames…");
    void drainCaptureQueue();
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
          {motionMode || queueDepth > 0 ? (
            <dl className="motion-status">
              <div>
                <dt>Motion state</dt>
                <dd>{motionMode ? (motionPhase === "idle" ? "watching" : motionPhase) : "paused"}</dd>
              </div>
              <div>
                <dt>Delivery</dt>
                <dd>{deliveryActive ? "uploading" : `${queueDepth} queued`}</dd>
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
            {running && !motionMode ? (
              <button type="button" className="button--quiet" onClick={startMotionMode} disabled={busy}>
                Start motion mode
              </button>
            ) : null}
            {running && motionMode ? (
              <button type="button" className="button--quiet" onClick={useManualMode}>
                Use manual mode
              </button>
            ) : null}
            <button type="button" onClick={sendSnapshot} disabled={!running || busy || motionMode}>
              {busy && running ? "Sending…" : "Send snapshot"}
            </button>
            {queueDepth > 0 && !deliveryActive ? (
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
