import { type RefObject, useEffect, useRef, useState } from "react";
import {
  uploadCapture,
  type BrowserCamera,
  type BrowserCaptureMetadata,
  type CaptureAccepted,
} from "./api";
import { captureFrame, sampleMotionFrame } from "./cameraFrames";
import {
  advanceMotionCadence,
  analyseMotion,
  initialMotionCadenceState,
  type MotionCaptureDecision,
  type MotionPhase,
} from "./motion";

const MOTION_SAMPLE_INTERVAL_MS = 250;
export const MAX_MEMORY_QUEUE_DEPTH = 30;

interface QueuedCapture {
  image: Blob;
  idempotencyKey: string;
  metadata: BrowserCaptureMetadata;
}

interface MotionCaptureOptions {
  videoRef: RefObject<HTMLVideoElement | null>;
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
  start: () => void;
  stop: () => void;
  retry: () => void;
}

export function useMotionCapture({
  videoRef,
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
  const queueRef = useRef<QueuedCapture[]>([]);
  const [active, setActive] = useState(false);
  const [phase, setPhase] = useState<MotionPhase>("idle");
  const [queueDepth, setQueueDepth] = useState(0);
  const [delivering, setDelivering] = useState(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      if (timerRef.current !== null) window.clearInterval(timerRef.current);
    };
  }, []);

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

  const publishQueueDepth = () => {
    if (mountedRef.current) setQueueDepth(queueRef.current.length);
  };

  const drainQueue = async () => {
    if (deliveryActiveRef.current || !camera) return;
    deliveryActiveRef.current = true;
    if (mountedRef.current) setDelivering(true);

    while (queueRef.current.length > 0) {
      const queued = queueRef.current[0];
      try {
        const accepted = await uploadCapture(
          camera.id,
          queued.image,
          queued.idempotencyKey,
          queued.metadata,
        );
        queueRef.current.shift();
        publishQueueDepth();
        if (mountedRef.current) {
          onAccepted(accepted);
          onMessage(`Motion frame stored. ${queueRef.current.length} queued for delivery.`);
        }
      } catch (error: unknown) {
        stop();
        if (mountedRef.current) {
          onMessage(
            `Motion capture paused because delivery failed: ${
              error instanceof Error ? error.message : "unknown upload error"
            }. The captured frames remain queued in this tab.`,
          );
        }
        break;
      }
    }

    deliveryActiveRef.current = false;
    if (mountedRef.current) setDelivering(false);
  };

  const enqueue = async (decision: MotionCaptureDecision) => {
    const video = videoRef.current;
    if (!video) return;
    if (queueRef.current.length >= MAX_MEMORY_QUEUE_DEPTH) {
      stop();
      onMessage(
        `Motion capture paused because the temporary queue reached ${MAX_MEMORY_QUEUE_DEPTH} frames. Keep this tab open and retry delivery.`,
      );
      return;
    }

    const frame = await captureFrame(video);
    queueRef.current.push({
      image: frame.image,
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
    });
    publishQueueDepth();
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
    onMessage("Retrying queued motion frames…");
    void drainQueue();
  };

  return { active, phase, queueDepth, delivering, start, stop, retry };
}
