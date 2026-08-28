import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { fileURLToPath } from "node:url";

const firebaseConfigPath = fileURLToPath(
  new URL("../../firebase.json", import.meta.url),
);

test("Firebase Hosting denies framing on every route", async () => {
  const config = JSON.parse(await readFile(firebaseConfigPath, "utf8"));
  const globalHeaders = config.hosting?.headers?.find(
    (entry) => entry.source === "**",
  )?.headers;

  assert.ok(globalHeaders, "a global Hosting header rule must exist");
  assert.deepEqual(
    globalHeaders.find(({ key }) => key === "Content-Security-Policy"),
    {
      key: "Content-Security-Policy",
      value: "frame-ancestors 'none'",
    },
  );
  assert.deepEqual(
    globalHeaders.find(({ key }) => key === "X-Frame-Options"),
    { key: "X-Frame-Options", value: "DENY" },
  );
});
