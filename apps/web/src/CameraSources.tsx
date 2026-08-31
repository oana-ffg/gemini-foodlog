import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";
import {
  API_BASE_URL,
  createDeviceCamera,
  getDeviceSnapshotRequest,
  listCameras,
  requestDeviceSnapshot,
  revokeCamera,
  type Account,
  type Camera,
  type DeviceCameraCredentialIssue,
} from "./api";

interface CameraSourcesProps {
  account: Account | undefined;
  currentBrowserCameraId: string | undefined;
  onCurrentBrowserRevoked: () => void;
}

function activityLabel(camera: Camera): string {
  if (!camera.last_capture_at) return "No images received yet";
  return `Last image ${new Date(camera.last_capture_at).toLocaleString()}`;
}

function deviceConfiguration(issue: DeviceCameraCredentialIssue) {
  return {
    api_base_url: API_BASE_URL,
    capture_endpoint: `${API_BASE_URL}/v1/captures`,
    status_endpoint: `${API_BASE_URL}/v1/device/status`,
    snapshot_poll_endpoint: `${API_BASE_URL}/v1/device/snapshot-request`,
    camera_id: issue.camera.id,
    authorization: `FoodLogCamera ${issue.credential}`,
  };
}

export default function CameraSources({
  account,
  currentBrowserCameraId,
  onCurrentBrowserRevoked,
}: CameraSourcesProps) {
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [deviceName, setDeviceName] = useState("");
  const [issued, setIssued] = useState<DeviceCameraCredentialIssue>();
  const [busyAction, setBusyAction] = useState<string>();
  const [message, setMessage] = useState("Loading camera sources…");
  const snapshotRunRef = useRef(0);

  const refresh = useCallback(async () => {
    try {
      const next = await listCameras();
      setCameras(next);
      setMessage(next.length === 0 ? "No camera sources yet." : "Camera sources are up to date.");
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Could not load camera sources.");
    }
  }, []);

  useEffect(() => {
    void refresh();
    return () => {
      snapshotRunRef.current += 1;
    };
  }, [refresh]);

  const registerDevice = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const name = deviceName.trim();
    if (!name) return;
    setBusyAction("device");
    setIssued(undefined);
    setMessage("Issuing a physical-camera credential…");
    try {
      const result = await createDeviceCamera(name);
      setIssued(result);
      setDeviceName("");
      await refresh();
      setMessage("Physical camera created. Save its credential now; it is shown only once.");
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Physical camera creation failed.");
    } finally {
      setBusyAction(undefined);
    }
  };

  const revoke = async (camera: Camera) => {
    if (!window.confirm(`Revoke ${camera.name}? It will immediately lose upload access.`)) {
      return;
    }
    setBusyAction(camera.id);
    setMessage(`Revoking ${camera.name}…`);
    try {
      await revokeCamera(camera.id);
      if (camera.id === currentBrowserCameraId) onCurrentBrowserRevoked();
      await refresh();
      setMessage(`${camera.name} is revoked and can no longer upload.`);
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Camera revocation failed.");
    } finally {
      setBusyAction(undefined);
    }
  };

  const copyDeviceConfiguration = async () => {
    if (!issued) return;
    try {
      await navigator.clipboard.writeText(JSON.stringify(deviceConfiguration(issued), null, 2));
      setMessage("Physical-camera configuration copied.");
    } catch {
      setMessage("Clipboard access failed. Select and copy the configuration manually.");
    }
  };

  const downloadDeviceConfiguration = () => {
    if (!issued) return;
    const blob = new Blob(
      [JSON.stringify(deviceConfiguration(issued), null, 2)],
      { type: "application/json" },
    );
    const href = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = href;
    anchor.download = `foodlog-camera-${issued.camera.id}.json`;
    anchor.click();
    URL.revokeObjectURL(href);
    setMessage("Camera setup file downloaded. Keep it private until setup is complete.");
  };

  const requestSnapshot = async (camera: DeviceCameraCredentialIssue["camera"]) => {
    const run = snapshotRunRef.current + 1;
    snapshotRunRef.current = run;
    setBusyAction(`snapshot:${camera.id}`);
    setMessage(`Asking ${camera.name} for one private snapshot…`);
    try {
      let request = await requestDeviceSnapshot(camera.id);
      for (let attempt = 0; attempt < 45 && request.status === "pending"; attempt += 1) {
        await new Promise((resolve) => window.setTimeout(resolve, 2_000));
        if (snapshotRunRef.current !== run) return;
        request = await getDeviceSnapshotRequest(camera.id, request.id);
      }
      if (snapshotRunRef.current !== run) return;
      if (request.status === "completed") {
        await refresh();
        if (snapshotRunRef.current !== run) return;
        setMessage(`${camera.name} uploaded the requested snapshot successfully.`);
      } else if (request.status === "expired") {
        setMessage(`${camera.name} did not collect the request before it expired.`);
      } else {
        setMessage(`The request is still queued for ${camera.name}; it may be offline.`);
      }
    } catch (error: unknown) {
      if (snapshotRunRef.current !== run) return;
      setMessage(error instanceof Error ? error.message : "Snapshot request failed.");
    } finally {
      if (snapshotRunRef.current === run) setBusyAction(undefined);
    }
  };

  return (
    <section className="camera-sources" aria-labelledby="camera-sources-title">
      <div className="section-heading">
        <div>
          <p className="eyebrow">Account-owned inputs</p>
          <h2 id="camera-sources-title">Camera sources</h2>
        </div>
        <span>{account?.accepted_image_count ?? 0} images accepted</span>
      </div>

      <div className="camera-source-forms">
        <div>
          <h3>Use this phone or browser</h3>
          <p>
            {currentBrowserCameraId
              ? "Connected automatically on this browser."
              : "Connecting this browser automatically…"}
          </p>
        </div>

        <form onSubmit={registerDevice}>
          <h3>Add a physical camera</h3>
          <p>
            Currently supports the{" "}
            <a
              href="https://github.com/Freenove/Freenove_ESP32_S3_WROOM_Board"
              target="_blank"
              rel="noreferrer"
            >
              Freenove FNK0085 ESP32-S3 WROOM camera
            </a>
            .{" "}
            <a href="/downloads/foodlog-camera-setup.zip" download>
              Download the FoodLog camera setup utility
            </a>
          </p>
          <label>
            Source name
            <input
              value={deviceName}
              onChange={(event) => setDeviceName(event.target.value)}
              maxLength={80}
              required
              autoComplete="off"
            />
          </label>
          <button type="submit" disabled={busyAction !== undefined || !deviceName.trim()}>
            {busyAction === "device" ? "Creating…" : "Create physical camera"}
          </button>
        </form>
      </div>

      {issued ? (
        <div className="device-credential" role="status">
          <strong>Save this credential now—it cannot be shown again.</strong>
          <p>
            Download the private setup file, then use it with the camera setup utility.
            Do not email or share it.
          </p>
          <textarea
            readOnly
            aria-label="Physical camera configuration"
            value={JSON.stringify(deviceConfiguration(issued), null, 2)}
            rows={8}
          />
          <div className="button-row">
            <button type="button" onClick={copyDeviceConfiguration}>Copy configuration</button>
            <button type="button" onClick={downloadDeviceConfiguration}>
              Download setup file
            </button>
            <button type="button" className="button--quiet" onClick={() => setIssued(undefined)}>
              I saved it; hide credential
            </button>
          </div>
        </div>
      ) : null}

      <p className="form-message" role="status">{message}</p>
      <div className="camera-source-list">
        {cameras.map((camera) => (
          <article key={camera.id} className="camera-source-card">
            <div>
              <span className={`camera-source-status camera-source-status--${camera.status}`}>
                {camera.status}
              </span>
              <span className="camera-source-card__kind">
                {camera.kind === "browser" ? "Phone / browser" : "Physical camera"}
              </span>
              <h3>{camera.name}</h3>
              <p>{activityLabel(camera)} · {camera.accepted_capture_count} accepted</p>
              <p className="fine-print">Created {new Date(camera.created_at).toLocaleString()}</p>
            </div>
            {camera.status === "active" ? (
              <div className="button-row button-row--compact">
                {camera.kind === "device" ? (
                  <button
                    type="button"
                    disabled={busyAction !== undefined}
                    onClick={() => void requestSnapshot(camera)}
                  >
                    {busyAction === `snapshot:${camera.id}` ? "Taking snapshot…" : "Take snapshot"}
                  </button>
                ) : null}
                <button
                  type="button"
                  className="button--danger"
                  disabled={busyAction !== undefined}
                  onClick={() => void revoke(camera)}
                >
                  {busyAction === camera.id ? "Revoking…" : "Revoke"}
                </button>
              </div>
            ) : null}
          </article>
        ))}
      </div>
    </section>
  );
}
