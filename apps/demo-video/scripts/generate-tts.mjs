import {createHash} from "node:crypto";
import {execFile} from "node:child_process";
import {mkdir, readFile, rename, writeFile} from "node:fs/promises";
import {fileURLToPath} from "node:url";
import path from "node:path";
import {promisify} from "node:util";

const execFileAsync = promisify(execFile);
const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const videoRoot = path.resolve(scriptDirectory, "..");
const contentPath = path.join(videoRoot, "src", "content.json");
const manifestPath = path.join(videoRoot, "src", "generated", "tts-manifest.json");
const audioDirectory = path.join(videoRoot, "public", "generated", "tts");
const model = "gpt-4o-mini-tts-2025-12-15";
const voice = "marin";
const gopassEntry = "openai/api-key";
const selectedScene = process.argv.find((value) => value.startsWith("--scene="))?.split("=", 2)[1];
const force = process.argv.includes("--force");

const content = JSON.parse(await readFile(contentPath, "utf8"));
const existingManifest = JSON.parse(await readFile(manifestPath, "utf8"));
const scenes = selectedScene
  ? content.scenes.filter((scene) => scene.id === selectedScene)
  : content.scenes;

if (scenes.length === 0) {
  throw new Error(`Unknown scene: ${selectedScene}`);
}

const globalDirection = [
  "Use a natural adult feminine voice with emotional intelligence and excellent English diction.",
  "Sound like one thoughtful person sharing something they genuinely built and care about.",
  "Keep the delivery intimate, conversational, and grounded; never use a glossy commercial announcer voice.",
  "Use natural pauses and varied intonation. Do not add words, sound effects, or laughter.",
].join(" ");

const specs = scenes.map((scene) => {
  const instructions = `${globalDirection} Scene direction: ${scene.voiceDirection}`;
  const hash = createHash("sha256")
    .update(JSON.stringify({model, voice, input: scene.narration, instructions}))
    .digest("hex");
  return {
    scene,
    instructions,
    hash,
    file: `generated/tts/${scene.id}-${hash.slice(0, 12)}.wav`,
  };
});

const pending = specs.filter(({scene, hash}) => {
  const entry = existingManifest.entries[scene.id];
  return force || !entry || entry.hash !== hash;
});

if (pending.length === 0) {
  console.log("All selected narration files already match their text, voice, model, and direction.");
  process.exit(0);
}

const {stdout: secretOutput} = await execFileAsync("gopass", ["show", "-o", gopassEntry], {
  maxBuffer: 16 * 1024,
});
const apiKey = secretOutput.trim();
if (!apiKey) {
  throw new Error(`No API key returned by gopass entry ${gopassEntry}`);
}

await mkdir(audioDirectory, {recursive: true});
const nextEntries = {...existingManifest.entries};

for (const spec of pending) {
  console.log(`Generating narration: ${spec.scene.id}`);
  const response = await fetch("https://api.openai.com/v1/audio/speech", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model,
      voice,
      input: spec.scene.narration,
      instructions: spec.instructions,
      response_format: "wav",
    }),
  });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`OpenAI speech request failed for ${spec.scene.id}: HTTP ${response.status} ${body.slice(0, 500)}`);
  }
  const bytes = Buffer.from(await response.arrayBuffer());
  if (bytes.length < 44 || bytes.subarray(0, 4).toString("ascii") !== "RIFF") {
    throw new Error(`OpenAI returned an invalid WAV for ${spec.scene.id}`);
  }
  const destination = path.join(videoRoot, "public", spec.file);
  const temporary = `${destination}.tmp`;
  await writeFile(temporary, bytes, {mode: 0o600});
  await rename(temporary, destination);
  const {stdout} = await execFileAsync("ffprobe", [
    "-v",
    "error",
    "-show_entries",
    "format=duration",
    "-of",
    "default=noprint_wrappers=1:nokey=1",
    destination,
  ]);
  const durationSeconds = Number.parseFloat(stdout.trim());
  if (!Number.isFinite(durationSeconds) || durationSeconds <= 0) {
    throw new Error(`Could not measure narration duration for ${spec.scene.id}`);
  }
  nextEntries[spec.scene.id] = {
    hash: spec.hash,
    file: spec.file,
    durationSeconds,
    characters: spec.scene.narration.length,
  };
}

for (const scene of content.scenes) {
  if (!nextEntries[scene.id]) {
    continue;
  }
  const currentSpec = specs.find((candidate) => candidate.scene.id === scene.id);
  if (currentSpec && nextEntries[scene.id].hash !== currentSpec.hash) {
    delete nextEntries[scene.id];
  }
}

await writeFile(
  manifestPath,
  `${JSON.stringify({generatedAt: new Date().toISOString(), model, voice, entries: nextEntries}, null, 2)}\n`,
);
console.log(`Updated ${manifestPath} without writing the API key to disk or logs.`);
