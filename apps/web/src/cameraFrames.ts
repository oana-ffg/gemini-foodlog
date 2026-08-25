const MAX_CAPTURE_EDGE = 1920;
const MOTION_SAMPLE_WIDTH = 64;
const MOTION_SAMPLE_HEIGHT = 48;

export interface CapturedFrame {
  image: Blob;
  width: number;
  height: number;
}

export function outputDimensions(
  width: number,
  height: number,
): { width: number; height: number } {
  const scale = Math.min(1, MAX_CAPTURE_EDGE / Math.max(width, height));
  return {
    width: Math.max(1, Math.round(width * scale)),
    height: Math.max(1, Math.round(height * scale)),
  };
}

export async function captureFrame(video: HTMLVideoElement): Promise<CapturedFrame> {
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

export function sampleMotionFrame(
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
