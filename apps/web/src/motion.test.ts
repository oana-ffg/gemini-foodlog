import { describe, expect, it } from "vitest";
import {
  advanceMotionCadence,
  analyseMotion,
  initialMotionCadenceState,
  MOTION_ALGORITHM,
  type MotionAnalysis,
  type MotionCadenceState,
} from "./motion";

function rgba(values: number[]): Uint8ClampedArray {
  return new Uint8ClampedArray(values.flatMap((value) => [value, value, value, 255]));
}

const still: MotionAnalysis = {
  detected: false,
  algorithm: MOTION_ALGORITHM,
  score: 0,
  changedPixelRatio: 0,
  threshold: 0.03,
};

const moving: MotionAnalysis = {
  detected: true,
  algorithm: MOTION_ALGORITHM,
  score: 0.3,
  changedPixelRatio: 0.4,
  threshold: 0.03,
};

describe("analyseMotion", () => {
  it("reports the changed-pixel ratio and normalized luma score", () => {
    const result = analyseMotion(
      rgba([0, 0, 0, 0]),
      rgba([255, 0, 255, 0]),
      {
        pixelLumaDeltaThreshold: 0.1,
        changedPixelRatioThreshold: 0.4,
      },
    );

    expect(result.detected).toBe(true);
    expect(result.changedPixelRatio).toBe(0.5);
    expect(result.score).toBeCloseTo(0.5);
    expect(result.threshold).toBe(0.4);
  });

  it("rejects mismatched or empty frame buffers", () => {
    expect(() => analyseMotion(new Uint8ClampedArray(), new Uint8ClampedArray()))
      .toThrow("equally sized");
    expect(() => analyseMotion(rgba([0]), rgba([0, 0]))).toThrow("equally sized");
  });
});

describe("advanceMotionCadence", () => {
  const ids = ["burst-one", "burst-two"];
  let idIndex = 0;
  const createBurstId = () => ids[idIndex++];

  function advance(
    state: MotionCadenceState,
    now: number,
    motion: MotionAnalysis,
  ) {
    return advanceMotionCadence(state, now, motion, createBurstId);
  }

  it("captures immediately and then at most once per second during a motion burst", () => {
    idIndex = 0;
    let result = advance(initialMotionCadenceState(), 1_000, moving);
    expect(result.capture).toMatchObject({ burstId: "burst-one", burstFrameIndex: 0 });

    result = advance(result.state, 1_500, moving);
    expect(result.capture).toBeUndefined();

    result = advance(result.state, 2_000, still);
    expect(result.capture).toMatchObject({ burstId: "burst-one", burstFrameIndex: 1 });
  });

  it("extends the burst on new motion, then samples once per minute while activity is open", () => {
    idIndex = 0;
    let result = advance(initialMotionCadenceState(), 0, moving);
    result = advance(result.state, 14_000, moving);
    expect(result.state.burstUntil).toBe(29_000);

    result = advance(result.state, 29_001, still);
    expect(result.state.phase).toBe("monitoring");
    expect(result.capture).toBeUndefined();

    result = advance(result.state, 89_001, still);
    expect(result.capture).toEqual({ motion: still });
    expect(result.state.phase).toBe("monitoring");
  });

  it("starts a new burst after monitoring and closes after inactivity", () => {
    idIndex = 0;
    let result = advance(initialMotionCadenceState(), 0, moving);
    result = advance(result.state, 15_001, still);
    result = advance(result.state, 16_000, moving);
    expect(result.capture).toMatchObject({ burstId: "burst-two", burstFrameIndex: 0 });

    result = advance(result.state, 31_001, still);
    result = advance(result.state, 316_000, still);
    expect(result.state).toEqual(initialMotionCadenceState());
    expect(result.capture).toBeUndefined();
  });
});
