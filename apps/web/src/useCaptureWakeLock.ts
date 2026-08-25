import { useEffect, useRef, useState } from "react";
import {
  CaptureWakeLockController,
  type CaptureWakeLockStatus,
  type ScreenWakeLockProvider,
} from "./wakeLock";

function browserWakeLockProvider(): ScreenWakeLockProvider | undefined {
  const wakeLock = (
    navigator as Navigator & { wakeLock?: ScreenWakeLockProvider }
  ).wakeLock;
  return wakeLock;
}

export function useCaptureWakeLock(requested: boolean): CaptureWakeLockStatus {
  const [status, setStatus] = useState<CaptureWakeLockStatus>("inactive");
  const controllerRef = useRef<CaptureWakeLockController | null>(null);

  useEffect(() => {
    const controller = new CaptureWakeLockController(
      browserWakeLockProvider(),
      document,
      setStatus,
    );
    controllerRef.current = controller;
    return () => {
      controller.dispose();
      controllerRef.current = null;
    };
  }, []);

  useEffect(() => {
    if (requested) controllerRef.current?.start();
    else controllerRef.current?.stop();
  }, [requested]);

  return status;
}
