import assert from "node:assert/strict";
import { after, before, test } from "node:test";
import { readFile } from "node:fs/promises";

import {
  assertFails,
  initializeTestEnvironment,
} from "@firebase/rules-unit-testing";
import {
  collection,
  collectionGroup,
  deleteDoc,
  doc,
  getDoc,
  getDocs,
  setLogLevel,
  setDoc,
  updateDoc,
} from "firebase/firestore";

const PROJECT_ID = "gemini-foodlog-rules-test";
const RULES_PATH = new URL("./firestore.rules", import.meta.url);
const DOCUMENT_PATHS = [
  "system/public_capacity",
  "identities/firebase-user-a",
  "device_credentials/credential-hash-a",
  "inbound_mail_routes/recipient-hash-a",
  "waitlist/email-hash-a",
  "outbox/message-a",
  "accounts/account-a",
  "accounts/account-a/entitlements/current",
  "accounts/account-a/cameras/camera-a",
  "accounts/account-a/inbound_mail_addresses/current",
  "accounts/account-a/capture_idempotency/idempotency-hash-a",
  "accounts/account-a/captures/capture-a",
  "accounts/account-a/media/media-a",
  "accounts/account-a/segments/segment-a",
  "accounts/account-a/events/event-a",
  "accounts/account-a/event_heads/camera-a",
  "accounts/account-a/meals/meal-a",
  "accounts/account-a/meals/meal-a/revisions/revision-a",
  "accounts/account-a/questions/question-a",
  "accounts/account-a/feedback/feedback-a",
  "accounts/account-a/knowledge/knowledge-a",
  "accounts/account-a/purchases/purchase-a",
  "accounts/account-a/purchase_identities/identity-a",
  "accounts/account-a/purchase_documents/mail-a",
  "accounts/account-a/purchase_normalizations/mail-a",
  "accounts/account-a/purchase_items/item-a",
  "accounts/account-a/purchase_charges/charge-a",
  "accounts/account-a/purchase_reconciliations/purchase-a",
  "accounts/account-a/raw_mail/mail-a",
  "accounts/account-a/traces/trace-a",
  "accounts/account-a/jobs/job-a",
  "accounts/account-a/exports/export-a",
  "accounts/account-a/consents/consent-a",
];
const COLLECTION_GROUPS = [
  "cameras",
  "inbound_mail_addresses",
  "captures",
  "events",
  "meals",
  "questions",
  "knowledge",
  "purchases",
  "purchase_identities",
  "purchase_documents",
  "purchase_normalizations",
  "purchase_items",
  "purchase_charges",
  "purchase_reconciliations",
  "jobs",
];

let testEnvironment;

setLogLevel("silent");

function emulatorAddress() {
  const value = process.env.FIRESTORE_EMULATOR_HOST;
  assert(value, "FIRESTORE_EMULATOR_HOST must be set by Firebase emulators:exec");
  const separator = value.lastIndexOf(":");
  assert.notEqual(separator, -1, "FIRESTORE_EMULATOR_HOST must include a port");
  return {
    host: value.slice(0, separator),
    port: Number.parseInt(value.slice(separator + 1), 10),
  };
}

function alternateDocumentPath(path) {
  const parts = path.split("/");
  parts[parts.length - 1] = `${parts.at(-1)}-direct-client-attempt`;
  return parts.join("/");
}

function parentCollectionPath(path) {
  return path.split("/").slice(0, -1).join("/");
}

before(async () => {
  const { host, port } = emulatorAddress();
  testEnvironment = await initializeTestEnvironment({
    projectId: PROJECT_ID,
    firestore: {
      host,
      port,
      rules: await readFile(RULES_PATH, "utf8"),
    },
  });
  await testEnvironment.withSecurityRulesDisabled(async (context) => {
    await Promise.all(
      DOCUMENT_PATHS.map((path) =>
        setDoc(doc(context.firestore(), path), {
          schema_version: 1,
          seeded_for_rules_test: true,
        }),
      ),
    );
  });
});

after(async () => {
  await testEnvironment?.cleanup();
});

const CLIENT_CONTEXTS = [
  ["unauthenticated", () => testEnvironment.unauthenticatedContext()],
  [
    "authenticated owner claims",
    () =>
      testEnvironment.authenticatedContext("firebase-user-a", {
        account_id: "account-a",
        email_verified: true,
      }),
  ],
  [
    "authenticated foreign and admin-like claims",
    () =>
      testEnvironment.authenticatedContext("firebase-user-b", {
        account_id: "account-b",
        admin: true,
        email_verified: true,
      }),
  ],
];

for (const [label, createContext] of CLIENT_CONTEXTS) {
  test(`${label} cannot directly read, query, create, update, or delete private data`, async () => {
    const database = createContext().firestore();

    for (const path of DOCUMENT_PATHS) {
      const reference = doc(database, path);
      await assertFails(getDoc(reference));
      await assertFails(getDocs(collection(database, parentCollectionPath(path))));
      await assertFails(
        setDoc(doc(database, alternateDocumentPath(path)), {
          attempted_direct_write: true,
        }),
      );
      await assertFails(updateDoc(reference, { attempted_direct_update: true }));
      await assertFails(deleteDoc(reference));
    }

    for (const groupName of COLLECTION_GROUPS) {
      await assertFails(getDocs(collectionGroup(database, groupName)));
    }
  });
}

test("denied client operations never changed emulator data", async () => {
  await testEnvironment.withSecurityRulesDisabled(async (context) => {
    const database = context.firestore();
    for (const path of DOCUMENT_PATHS) {
      const snapshot = await getDoc(doc(database, path));
      assert.equal(snapshot.exists(), true, `${path} must still exist`);
      assert.deepEqual(snapshot.data(), {
        schema_version: 1,
        seeded_for_rules_test: true,
      });
      const attempted = await getDoc(doc(database, alternateDocumentPath(path)));
      assert.equal(attempted.exists(), false, `${path} create attempt must not persist`);
    }
  });
});
