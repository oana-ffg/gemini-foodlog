import {execFile} from "node:child_process";
import {mkdtemp, readFile, rm, writeFile} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import {fileURLToPath, pathToFileURL} from "node:url";
import {promisify} from "node:util";

const execFileAsync = promisify(execFile);
const repositoryRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const sourcePath = path.join(repositoryRoot, "docs", "architecture-diagram.mmd");
const outputPath = path.join(repositoryRoot, "docs", "architecture-diagram.png");
const mermaidPath = path.join(repositoryRoot, "node_modules", "mermaid", "dist", "mermaid.min.js");

const chromeCandidates = [
  process.env.FOODLOG_CHROME_PATH,
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/google-chrome",
  "/usr/bin/chromium",
].filter(Boolean);

let chromePath;
for (const candidate of chromeCandidates) {
  try {
    await execFileAsync(candidate, ["--version"]);
    chromePath = candidate;
    break;
  } catch {
    // Continue to the next explicit candidate.
  }
}
if (!chromePath) {
  throw new Error("Chrome was not found. Set FOODLOG_CHROME_PATH to a Chromium-compatible executable.");
}

const source = await readFile(sourcePath, "utf8");
const temporaryDirectory = await mkdtemp(path.join(os.tmpdir(), "foodlog-architecture-"));
const htmlPath = path.join(temporaryDirectory, "architecture.html");
const html = `<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <style>
      html, body { margin: 0; width: 2400px; height: 700px; overflow: hidden; background: #fffdf7; }
      body { display: grid; place-items: center; }
      #diagram { width: 2320px; height: 620px; display: grid; place-items: center; }
      #diagram svg { width: 2320px !important; height: 620px !important; max-width: none !important; }
    </style>
    <script src="${pathToFileURL(mermaidPath).href}"></script>
  </head>
  <body>
    <div id="diagram"></div>
    <script>
      mermaid.initialize({startOnLoad: false, theme: "neutral", securityLevel: "strict"});
      mermaid.render("foodlog-architecture", ${JSON.stringify(source)}).then(({svg}) => {
        document.getElementById("diagram").innerHTML = svg;
      });
    </script>
  </body>
</html>`;

try {
  await writeFile(htmlPath, html, "utf8");
  await execFileAsync(chromePath, [
    "--headless=new",
    "--hide-scrollbars",
    "--allow-file-access-from-files",
    "--disable-gpu",
    "--virtual-time-budget=5000",
    "--window-size=2400,700",
    `--screenshot=${outputPath}`,
    pathToFileURL(htmlPath).href,
  ]);
} finally {
  await rm(temporaryDirectory, {recursive: true, force: true});
}

console.log(`Rendered ${outputPath}`);
