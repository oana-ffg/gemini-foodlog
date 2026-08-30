import {createHash} from "node:crypto";
import {execFile} from "node:child_process";
import {readFile} from "node:fs/promises";
import path from "node:path";
import {promisify} from "node:util";

const execFileAsync = promisify(execFile);
const input = process.argv.find((argument) => !argument.startsWith("--") && argument.endsWith(".mp4"));
const allowSilent = process.argv.includes("--allow-silent");
if (!input) {
  throw new Error("Usage: node scripts/verify-render.mjs <video.mp4> [--allow-silent]");
}
const videoPath = path.resolve(input);

const {stdout: probeOutput} = await execFileAsync("ffprobe", [
  "-v",
  "error",
  "-show_entries",
  "format=duration,size:stream=index,codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
  "-of",
  "json",
  videoPath,
]);
const probe = JSON.parse(probeOutput);
const video = probe.streams.find((stream) => stream.codec_type === "video");
const audio = probe.streams.find((stream) => stream.codec_type === "audio");
const durationSeconds = Number(probe.format.duration);

if (!video || video.codec_name !== "h264" || video.width !== 1920 || video.height !== 1080 || video.r_frame_rate !== "30/1") {
  throw new Error(`Unexpected video stream: ${JSON.stringify(video)}`);
}
if (!audio || audio.codec_name !== "aac") {
  throw new Error(`Expected an AAC audio stream, received: ${JSON.stringify(audio)}`);
}
if (!Number.isFinite(durationSeconds) || durationSeconds <= 0 || durationSeconds > 240) {
  throw new Error(`Duration must be positive and no more than four minutes, received ${durationSeconds}`);
}

const {stderr: blackOutput} = await execFileAsync("ffmpeg", [
  "-hide_banner",
  "-i",
  videoPath,
  "-vf",
  "blackdetect=d=0.5:pix_th=0.02",
  "-an",
  "-f",
  "null",
  "-",
], {maxBuffer: 16 * 1024 * 1024});
if (/black_start:/.test(blackOutput)) {
  throw new Error("Render contains a black interval of at least 0.5 seconds");
}

if (!allowSilent) {
  const {stderr: silenceOutput} = await execFileAsync("ffmpeg", [
    "-hide_banner",
    "-i",
    videoPath,
    "-af",
    "silencedetect=noise=-50dB:d=5",
    "-vn",
    "-f",
    "null",
    "-",
  ], {maxBuffer: 16 * 1024 * 1024});
  if (/silence_duration:/.test(silenceOutput)) {
    throw new Error("Final render contains an audio silence interval of at least five seconds");
  }
}

const bytes = await readFile(videoPath);
const sha256 = createHash("sha256").update(bytes).digest("hex");
console.log(JSON.stringify({
  path: videoPath,
  durationSeconds,
  bytes: bytes.length,
  sha256,
  video: {codec: video.codec_name, width: video.width, height: video.height, fps: video.r_frame_rate},
  audio: {codec: audio.codec_name, sampleRate: audio.sample_rate, channels: audio.channels},
  allowedSilentDraft: allowSilent,
}, null, 2));
