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

const MAX_CAPTURE_EDGE = 1920;

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
      setMessage("Camera is running. Use Send snapshot when the frame is useful.");
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Camera permission failed.");
    } finally {
      setBusy(false);
    }
  };

  const pauseCamera = () => {
    stopStream(streamRef.current);
    streamRef.current = null;
    if (videoRef.current) videoRef.current.srcObject = null;
    setRunning(false);
    setMessage("Camera paused. No images are being captured or uploaded.");
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
      setAccount((current) => current ? {
        ...current,
        accepted_image_count: accepted.accepted_image_count,
        entitlement_mode: accepted.entitlement_mode,
        trial_image_limit: accepted.trial_image_limit,
      } : current);
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

  return (
    <main className="camera-page">
      <header className="camera-page__header">
        <div>
          <p className="eyebrow">Manual browser camera</p>
          <h1>Send one real kitchen snapshot.</h1>
          <p>Manual mode first. Motion mode and wake lock come after this path is proven.</p>
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
            <button type="button" onClick={sendSnapshot} disabled={!running || busy}>
              {busy && running ? "Sending…" : "Send snapshot"}
            </button>
          </div>
          <p className="fine-print">
            Nothing is uploaded until you press Send snapshot. The full frame is scaled
            only when needed to fit the shared capture contract.
          </p>
        </div>
      </section>
    </main>
  );
}
