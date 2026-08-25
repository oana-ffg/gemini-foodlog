export const MOTION_ALGORITHM = "browser-luma-delta-v1";

export interface MotionDetectionConfig {
  pixelLumaDeltaThreshold: number;
  changedPixelRatioThreshold: number;
}

export interface MotionAnalysis {
  detected: boolean;
  algorithm: typeof MOTION_ALGORITHM;
  score: number;
  changedPixelRatio: number;
  threshold: number;
}

export const DEFAULT_MOTION_DETECTION: MotionDetectionConfig = {
  pixelLumaDeltaThreshold: 0.12,
  changedPixelRatioThreshold: 0.03,
};

export function analyseMotion(
  previousRgba: Uint8ClampedArray,
  currentRgba: Uint8ClampedArray,
  config: MotionDetectionConfig = DEFAULT_MOTION_DETECTION,
): MotionAnalysis {
  if (
    previousRgba.length === 0
    || previousRgba.length !== currentRgba.length
    || previousRgba.length % 4 !== 0
  ) {
    throw new Error("Motion frames must be equally sized, non-empty RGBA buffers.");
  }

  const pixelCount = currentRgba.length / 4;
  let changedPixels = 0;
  let totalLumaDelta = 0;

  for (let offset = 0; offset < currentRgba.length; offset += 4) {
    const previousLuma = (
      0.2126 * previousRgba[offset]
      + 0.7152 * previousRgba[offset + 1]
      + 0.0722 * previousRgba[offset + 2]
    ) / 255;
    const currentLuma = (
      0.2126 * currentRgba[offset]
      + 0.7152 * currentRgba[offset + 1]
      + 0.0722 * currentRgba[offset + 2]
    ) / 255;
    const delta = Math.abs(currentLuma - previousLuma);
    totalLumaDelta += delta;
    if (delta >= config.pixelLumaDeltaThreshold) changedPixels += 1;
  }

  const changedPixelRatio = changedPixels / pixelCount;
  return {
    detected: changedPixelRatio >= config.changedPixelRatioThreshold,
    algorithm: MOTION_ALGORITHM,
    score: totalLumaDelta / pixelCount,
    changedPixelRatio,
    threshold: config.changedPixelRatioThreshold,
  };
}

export interface MotionCadenceConfig {
  burstDurationMs: number;
  burstIntervalMs: number;
  monitoringIntervalMs: number;
  inactivityTimeoutMs: number;
}

export const DEFAULT_MOTION_CADENCE: MotionCadenceConfig = {
  burstDurationMs: 15_000,
  burstIntervalMs: 1_000,
  monitoringIntervalMs: 60_000,
  inactivityTimeoutMs: 5 * 60_000,
};

export type MotionPhase = "idle" | "burst" | "monitoring";

export interface MotionCadenceState {
  phase: MotionPhase;
  lastMotionAt: number | null;
  burstUntil: number | null;
  nextCaptureAt: number | null;
  burstId: string | null;
  nextBurstFrameIndex: number;
}

export interface MotionCaptureDecision {
  motion: MotionAnalysis;
  burstId?: string;
  burstFrameIndex?: number;
}

export interface MotionCadenceResult {
  state: MotionCadenceState;
  capture?: MotionCaptureDecision;
}

export function initialMotionCadenceState(): MotionCadenceState {
  return {
    phase: "idle",
    lastMotionAt: null,
    burstUntil: null,
    nextCaptureAt: null,
    burstId: null,
    nextBurstFrameIndex: 0,
  };
}

export function advanceMotionCadence(
  current: MotionCadenceState,
  now: number,
  motion: MotionAnalysis,
  createBurstId: () => string,
  config: MotionCadenceConfig = DEFAULT_MOTION_CADENCE,
): MotionCadenceResult {
  let state = current;

  if (motion.detected) {
    if (state.phase !== "burst" || state.burstUntil === null || now > state.burstUntil) {
      state = {
        phase: "burst",
        lastMotionAt: now,
        burstUntil: now + config.burstDurationMs,
        nextCaptureAt: now,
        burstId: createBurstId(),
        nextBurstFrameIndex: 0,
      };
    } else {
      state = {
        ...state,
        lastMotionAt: now,
        burstUntil: now + config.burstDurationMs,
      };
    }
  } else if (state.phase === "burst" && state.burstUntil !== null && now > state.burstUntil) {
    state = {
      ...state,
      phase: "monitoring",
      burstUntil: null,
      nextCaptureAt: now + config.monitoringIntervalMs,
      burstId: null,
      nextBurstFrameIndex: 0,
    };
  }

  if (
    state.phase === "monitoring"
    && state.lastMotionAt !== null
    && now - state.lastMotionAt >= config.inactivityTimeoutMs
  ) {
    return { state: initialMotionCadenceState() };
  }

  if (state.nextCaptureAt === null || now < state.nextCaptureAt) {
    return { state };
  }

  if (state.phase === "burst" && state.burstId !== null) {
    const capture = {
      motion,
      burstId: state.burstId,
      burstFrameIndex: state.nextBurstFrameIndex,
    };
    return {
      state: {
        ...state,
        nextCaptureAt: now + config.burstIntervalMs,
        nextBurstFrameIndex: state.nextBurstFrameIndex + 1,
      },
      capture,
    };
  }

  if (state.phase === "monitoring") {
    return {
      state: {
        ...state,
        nextCaptureAt: now + config.monitoringIntervalMs,
      },
      capture: { motion },
    };
  }

  return { state };
}
