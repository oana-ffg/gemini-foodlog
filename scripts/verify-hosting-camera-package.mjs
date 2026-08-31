import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const repositoryRoot = dirname(dirname(fileURLToPath(import.meta.url)));
const packageName = "foodlog-camera-setup.zip";
const sourcePath = join(repositoryRoot, "assets", "brand", "downloads", packageName);
const sidecarPath = `${sourcePath}.sha256`;
const builtPath = join(repositoryRoot, "apps", "web", "dist", "downloads", packageName);

function sha256(contents) {
  return createHash("sha256").update(contents).digest("hex");
}

const [source, sidecar, built] = await Promise.all([
  readFile(sourcePath),
  readFile(sidecarPath, "utf8"),
  readFile(builtPath),
]);

if (source.length < 100_000 || source[0] !== 0x50 || source[1] !== 0x4b) {
  throw new Error("The source camera setup package is not a plausible ZIP archive.");
}

const sidecarMatch = sidecar.trim().match(/^([a-f0-9]{64})\s+foodlog-camera-setup\.zip$/);
if (!sidecarMatch) {
  throw new Error("The camera setup package checksum sidecar has an invalid format.");
}

const sourceHash = sha256(source);
const builtHash = sha256(built);
if (sidecarMatch[1] !== sourceHash) {
  throw new Error("The camera setup package does not match its checksum sidecar.");
}
if (builtHash !== sourceHash || !built.equals(source)) {
  throw new Error("The built website does not contain the exact source camera setup package.");
}

console.log(`Verified hosted camera setup package: ${source.length} bytes, SHA-256 ${sourceHash}`);
