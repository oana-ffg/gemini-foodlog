import { describe, expect, it } from "vitest";
import { outputDimensions } from "./cameraFrames";

describe("outputDimensions", () => {
  it("preserves smaller frames and proportionally bounds larger frames", () => {
    expect(outputDimensions(1280, 720)).toEqual({ width: 1280, height: 720 });
    expect(outputDimensions(4032, 3024)).toEqual({ width: 1920, height: 1440 });
    expect(outputDimensions(3024, 4032)).toEqual({ width: 1440, height: 1920 });
  });
});
