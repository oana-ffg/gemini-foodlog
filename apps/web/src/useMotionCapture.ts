import { type RefObject, useEffect, useRef, useState } from "react";
import {
  uploadCapture,
  type BrowserCamera,
  type CaptureAccepted,
} from "./api";
import { captureFrame, sampleMotionFrame } from "./cameraFrames";
import { deliverOldestCapture } from "./captureDelivery";
import {
  IndexedDbCaptureQueue,
  captureQueueDatabaseName,
  type CaptureQueueStore,
  type PersistedCapture,
} from "./captureQueue";
import {
  advanceMotionCadence,
  analyseMotion,
  initialMotionCadenceState,
  type MotionCaptureDecision,
  type MotionPhase,
} from "./motion";

const MOTION_SAMPLE_INTERVAL_MS = 250;
export const MAX_MEMORY_QUEUE_DEPTH = 30;

interface MotionCaptureOptions {
  videoRef: RefObject<HTMLVideoElement | null>;
  ownerUserId: string;
  camera: BrowserCamera | undefined;
  sequenceId: string;
  takeSequenceNumber: () => number;
  onAccepted: (accepted: CaptureAccepted) => void;
  onMessage: (message: string) => void;
}

export interface MotionCaptureController {
  active: boolean;
  phase: MotionPhase;
  queueDepth: number;
  delivering: boolean;
  blockedReason?: string;
  start: () => void;
  stop: () => void;
  retry: () => void;
}

export function useMotionCapture({
  videoRef,
  ownerUserId,
  camera,
  sequenceId,
  takeSequenceNumber,
  onAccepted,
  onMessage,
}: MotionCaptureOptions): MotionCaptureController {
  const mountedRef = useRef(true);
  const activeRef = useRef(false);
  const timerRef = useRef<number | null>(null);
  const sampleBusyRef = useRef(false);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const previousFrameRef = useRef<Uint8ClampedArray | null>(null);
  const cadenceRef = useRef(initialMotionCadenceState());
  const deliveryActiveRef = useRef(false);
  const retryTimerRef = useRef<number | null>(null);
  const queueStoreRef = useRef<CaptureQueueStore | null>(null);
  if (queueStoreRef.current === null && typeof indexedDB !== "undefined") {
    queueStoreRef.current = new IndexedDbCaptureQueue(
      captureQueueDatabaseName(ownerUserId),
    );
  }
  const queueStore = queueStoreRef.current;
  const [active, setActive] = useState(false);
  const [phase, setPhase] = useState<MotionPhase>("idle");
  const [queueDepth, setQueueDepth] = useState(0);
  const [delivering, setDelivering] = useState(false);
  const [blockedReason, setBlockedReason] = useState<string>();

  const stop = () => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
    activeRef.current = false;
    previousFrameRef.current = null;
    cadenceRef.current = initialMotionCadenceState();
    if (mountedRef.current) {
      setActive(false);
      setPhase("idle");
    }
  };

  const publishQueueState = async () => {
    if (!queueStore) return;
    const [count, oldest] = await Promise.all([
      queueStore.count(),
      queueStore.oldest(),
    ]);
    if (mountedRef.current) {
      setQueueDepth(count);
      setBlockedReason(oldest?.status === "blocked" ? oldest.lastError : undefined);
    }
  };

  const scheduleRetry = (retryAt: number) => {
    if (retryTimerRef.current !== null) window.clearTimeout(retryTimerRef.current);
    retryTimerRef.current = window.setTimeout(() => {
      retryTimerRef.current = null;
      void drainQueue();
    }, Math.max(0, retryAt - Date.now()));
  };

  const drainQueue = async () => {
    if (deliveryActiveRef.current || !queueStore) return;
    deliveryActiveRef.current = true;
    if (mountedRef.current) setDelivering(true);

    try {
      while (true) {
        const result = await deliverOldestCapture(
          queueStore,
          (queued) => uploadCapture(
            queued.cameraId,
            queued.image,
            queued.idempotencyKey,
            queued.metadata,
          ),
          Date.now(),
        );
        await publishQueueState();

        if (result.kind === "delivered") {
          if (mountedRef.current) {
            onAccepted(result.accepted);
            onMessage("Motion frame stored. Continuing oldest-first delivery.");
          }
          continue;
        }
        if (result.kind === "waiting" || result.kind === "retry") {
          scheduleRetry(result.retryAt);
          if (result.kind === "retry" && mountedRef.current) {
            const delaySeconds = Math.max(1, Math.ceil((result.retryAt - Date.now()) / 1_000));
            onMessage(
              `Delivery is temporarily unavailable. Retrying the oldest frame in ${delaySeconds} seconds; new captures remain persistent.`,
            );
          }
          break;
        }
        if (result.kind === "blocked") {
          stop();
          if (mountedRef.current) {
            setBlockedReason(result.reason);
            onMessage(
              `Persistent delivery stopped: ${result.reason}. The queued frames remain on this device.`,
            );
          }
        }
        break;
      }
    } catch (error: unknown) {
      stop();
      if (mountedRef.current) {
        onMessage(
          error instanceof Error
            ? `The persistent capture queue failed: ${error.message}`
            : "The persistent capture queue failed.",
        );
      }
    } finally {
      deliveryActiveRef.current = false;
      if (mountedRef.current) setDelivering(false);
    }
  };

  const enqueue = async (decision: MotionCaptureDecision) => {
    const video = videoRef.current;
    if (!video || !camera || !queueStore) return;
    const storedCount = await queueStore.count();
    if (storedCount >= MAX_MEMORY_QUEUE_DEPTH) {
      stop();
      onMessage(
        `Motion capture paused because the persistent queue reached ${MAX_MEMORY_QUEUE_DEPTH} frames. Keep this page open for delivery or return later; the frames remain on this device.`,
      );
      return;
    }

    const frame = await captureFrame(video);
    const createdAt = Date.now();
    const capture: PersistedCapture = {
      image: frame.image,
      cameraId: camera.id,
      idempotencyKey: crypto.randomUUID(),
      metadata: {
        capturedAt: new Date().toISOString(),
        sequenceId,
        sequenceNumber: takeSequenceNumber(),
        width: frame.width,
        height: frame.height,
        burstId: decision.burstId,
        burstFrameIndex: decision.burstFrameIndex,
        motion: decision.motion,
      },
      createdAt,
      attempts: 0,
      nextAttemptAt: createdAt,
      status: "pending",
    };
    await queueStore.add(capture);
    await publishQueueState();
    void drainQueue();
  };

  const sample = async () => {
    const video = videoRef.current;
    if (
      !activeRef.current
      || sampleBusyRef.current
      || !video
      || video.videoWidth === 0
      || video.videoHeight === 0
    ) return;

    sampleBusyRef.current = true;
    try {
      canvasRef.current ??= document.createElement("canvas");
      const currentFrame = sampleMotionFrame(video, canvasRef.current);
      const previousFrame = previousFrameRef.current;
      previousFrameRef.current = currentFrame;
      if (!previousFrame) {
        onMessage("Motion mode is watching locally. Nothing has been uploaded yet.");
        return;
      }

      const analysis = analyseMotion(previousFrame, currentFrame);
      const result = advanceMotionCadence(
        cadenceRef.current,
        Date.now(),
        analysis,
        () => `motion-${crypto.randomUUID()}`,
      );
      cadenceRef.current = result.state;
      if (mountedRef.current) setPhase(result.state.phase);
      if (result.capture) await enqueue(result.capture);
    } catch (error: unknown) {
      stop();
      if (mountedRef.current) {
        onMessage(
          error instanceof Error ? error.message : "Motion detection stopped unexpectedly.",
        );
      }
    } finally {
      sampleBusyRef.current = false;
    }
  };

  const start = () => {
    if (!queueStore) {
      onMessage("This browser does not provide persistent capture storage.");
      return;
    }
    if (blockedReason) {
      onMessage(`Motion mode cannot start while delivery is blocked: ${blockedReason}.`);
      return;
    }
    if (!camera || !videoRef.current || activeRef.current) return;
    previousFrameRef.current = null;
    cadenceRef.current = initialMotionCadenceState();
    activeRef.current = true;
    setActive(true);
    setPhase("idle");
    onMessage("Starting local motion detection…");
    void sample();
    timerRef.current = window.setInterval(
      () => void sample(),
      MOTION_SAMPLE_INTERVAL_MS,
    );
  };

  const retry = () => {
    if (blockedReason) {
      onMessage(`Delivery remains blocked: ${blockedReason}.`);
      return;
    }
    if (retryTimerRef.current !== null) {
      window.clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
    onMessage("Retrying queued motion frames…");
    void drainQueue();
  };

  useEffect(() => {
    mountedRef.current = true;
    void publishQueueState().then(() => drainQueue()).catch((error: unknown) => {
      if (mountedRef.current) {
        onMessage(
          error instanceof Error
            ? `The persistent capture queue could not start: ${error.message}`
            : "The persistent capture queue could not start.",
        );
      }
    });
    return () => {
      mountedRef.current = false;
      if (timerRef.current !== null) window.clearInterval(timerRef.current);
      if (retryTimerRef.current !== null) window.clearTimeout(retryTimerRef.current);
    };
  }, []);

  return {
    active,
    phase,
    queueDepth,
    delivering,
    blockedReason,
    start,
    stop,
    retry,
  };
}
