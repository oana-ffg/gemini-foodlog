import {access, copyFile, mkdir, rm} from "node:fs/promises";
import {constants} from "node:fs";
import {execFile} from "node:child_process";
import {fileURLToPath} from "node:url";
import path from "node:path";
import {promisify} from "node:util";

const execFileAsync = promisify(execFile);
const scriptDirectory = path.dirname(fileURLToPath(import.meta.url));
const videoRoot = path.resolve(scriptDirectory, "..");
const repositoryRoot = path.resolve(videoRoot, "../..");
const generatedRoot = path.join(videoRoot, "public", "generated");
const screenshotTarget = path.join(generatedRoot, "screenshots");
const brandTarget = path.join(generatedRoot, "brand");
const videoTarget = path.join(generatedRoot, "video");
const hardwareTarget = path.join(generatedRoot, "hardware");
const privateShots = path.join(repositoryRoot, "artifacts", "demo-video", "private-shots");
const optionalVideoRoot = path.join(repositoryRoot, "artifacts", "demo-video", "veo");

const screenshots = [
  "01_timeline.png",
  "02_ambiguous_detail.png",
  "03_correction_history.png",
  "04_knowledge.png",
  "05_cat_discarded.png",
  "06_patterns.png",
  "07_cloud_proof.png",
];

const exists = async (filePath) => {
  try {
    await access(filePath, constants.R_OK);
    return true;
  } catch {
    return false;
  }
};

const dimensions = async (filePath) => {
  const {stdout} = await execFileAsync("ffprobe", [
    "-v",
    "error",
    "-select_streams",
    "v:0",
    "-show_entries",
    "stream=width,height",
    "-of",
    "csv=s=x:p=0",
    filePath,
  ]);
  return stdout.trim();
};

await mkdir(screenshotTarget, {recursive: true});
await mkdir(brandTarget, {recursive: true});
await mkdir(videoTarget, {recursive: true});
await mkdir(hardwareTarget, {recursive: true});

for (const name of screenshots) {
  const source = path.join(privateShots, name);
  if (!(await exists(source))) {
    throw new Error(`Missing reviewed production screenshot: ${source}`);
  }
  const size = await dimensions(source);
  const [width, height] = size.split("x").map(Number);
  if (width < 1920 || height < 1080) {
    throw new Error(`Expected ${name} to be at least 1920x1080, received ${size}`);
  }
  await copyFile(source, path.join(screenshotTarget, name));
}

await copyFile(
  path.join(repositoryRoot, "assets", "brand", "foodlog-mark.svg"),
  path.join(brandTarget, "foodlog-mark.svg"),
);

const hardwareSource = path.join(privateShots, "08_controller_hardware.jpg");
if (!(await exists(hardwareSource))) {
  throw new Error(`Missing reviewed physical-camera photo: ${hardwareSource}`);
}
const hardwareSize = await dimensions(hardwareSource);
const [hardwareWidth, hardwareHeight] = hardwareSize.split("x").map(Number);
if (hardwareWidth < 1920 || hardwareHeight < 1080) {
  throw new Error(`Expected controller photo to be at least 1920x1080, received ${hardwareSize}`);
}
await copyFile(hardwareSource, path.join(hardwareTarget, "controller-hardware.jpg"));

for (const name of ["intro-cooking.mp4", "intro-chaos.mp4"]) {
  const source = path.join(optionalVideoRoot, name);
  const target = path.join(videoTarget, name);
  if (await exists(source)) {
    await copyFile(source, target);
  } else {
    await rm(target, {force: true});
  }
}

console.log(`Prepared ${screenshots.length} reviewed production screenshots, one real hardware photo, and the canonical brand mark.`);
console.log(`Optional Veo clips: ${optionalVideoRoot}`);
