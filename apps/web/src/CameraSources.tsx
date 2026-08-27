import { type FormEvent, useCallback, useEffect, useState } from "react";
import {
  API_BASE_URL,
  createDeviceCamera,
  listCameras,
  revokeCamera,
  type Account,
  type BrowserCamera,
  type Camera,
  type DeviceCameraCredentialIssue,
} from "./api";

interface CameraSourcesProps {
  account: Account | undefined;
  currentBrowserCameraId: string | undefined;
  onRegisterBrowser: (name: string) => Promise<BrowserCamera>;
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
    camera_id: issue.camera.id,
    authorization: `FoodLogCamera ${issue.credential}`,
  };
}

export default function CameraSources({
  account,
  currentBrowserCameraId,
  onRegisterBrowser,
  onCurrentBrowserRevoked,
}: CameraSourcesProps) {
  const [cameras, setCameras] = useState<Camera[]>([]);
  const [browserName, setBrowserName] = useState("");
  const [deviceName, setDeviceName] = useState("");
  const [issued, setIssued] = useState<DeviceCameraCredentialIssue>();
  const [busyAction, setBusyAction] = useState<string>();
  const [message, setMessage] = useState("Loading camera sources…");

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
  }, [refresh]);

  const registerBrowser = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const name = browserName.trim();
    if (!name) return;
    setBusyAction("browser");
    setMessage("Registering this browser…");
    try {
      await onRegisterBrowser(name);
      setBrowserName("");
      await refresh();
      setMessage("This browser is ready for camera capture.");
    } catch (error: unknown) {
      setMessage(error instanceof Error ? error.message : "Browser registration failed.");
    } finally {
      setBusyAction(undefined);
    }
  };

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
        <form onSubmit={registerBrowser}>
          <h3>Use this phone or browser</h3>
          <label>
            Source name
            <input
              value={browserName}
              onChange={(event) => setBrowserName(event.target.value)}
              maxLength={80}
              required
              autoComplete="off"
            />
          </label>
          <button type="submit" disabled={busyAction !== undefined || !browserName.trim()}>
            {busyAction === "browser" ? "Registering…" : "Register this browser"}
          </button>
        </form>

        <form onSubmit={registerDevice}>
          <h3>Add a physical camera</h3>
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
          <textarea
            readOnly
            aria-label="Physical camera configuration"
            value={JSON.stringify(deviceConfiguration(issued), null, 2)}
            rows={8}
          />
          <div className="button-row">
            <button type="button" onClick={copyDeviceConfiguration}>Copy configuration</button>
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
              <button
                type="button"
                className="button--danger"
                disabled={busyAction !== undefined}
                onClick={() => void revoke(camera)}
              >
                {busyAction === camera.id ? "Revoking…" : "Revoke"}
              </button>
            ) : null}
          </article>
        ))}
      </div>
    </section>
  );
}
