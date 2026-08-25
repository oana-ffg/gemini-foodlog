import type { BrowserCaptureMetadata } from "./api";

const DATABASE_NAME = "foodlog-browser-capture-v1";
const DATABASE_VERSION = 1;
const STORE_NAME = "captures";
const CREATED_AT_INDEX = "by-created-at";

export function captureQueueDatabaseName(ownerUserId: string): string {
  if (!ownerUserId) throw new Error("Capture queue ownership is required.");
  return `${DATABASE_NAME}-${ownerUserId}`;
}

export type QueuedCaptureStatus = "pending" | "blocked";

export interface PersistedCapture {
  idempotencyKey: string;
  cameraId: string;
  image: Blob;
  metadata: BrowserCaptureMetadata;
  createdAt: number;
  attempts: number;
  nextAttemptAt: number;
  status: QueuedCaptureStatus;
  lastError?: string;
}

export interface CaptureQueueStore {
  add(capture: PersistedCapture): Promise<void>;
  count(): Promise<number>;
  oldest(): Promise<PersistedCapture | undefined>;
  put(capture: PersistedCapture): Promise<void>;
  remove(idempotencyKey: string): Promise<void>;
}

function requestResult<T>(request: IDBRequest<T>): Promise<T> {
  return new Promise((resolve, reject) => {
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("IndexedDB request failed."));
  });
}

function transactionDone(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onabort = () => reject(
      transaction.error ?? new Error("IndexedDB transaction was aborted."),
    );
    transaction.onerror = () => reject(
      transaction.error ?? new Error("IndexedDB transaction failed."),
    );
  });
}

export class IndexedDbCaptureQueue implements CaptureQueueStore {
  private databasePromise: Promise<IDBDatabase> | undefined;

  constructor(
    private readonly databaseName = DATABASE_NAME,
    private readonly factory: IDBFactory = indexedDB,
  ) {}

  async add(capture: PersistedCapture): Promise<void> {
    const database = await this.database();
    const transaction = database.transaction(STORE_NAME, "readwrite");
    transaction.objectStore(STORE_NAME).add(capture);
    await transactionDone(transaction);
  }

  async count(): Promise<number> {
    const database = await this.database();
    const transaction = database.transaction(STORE_NAME, "readonly");
    const result = await requestResult(transaction.objectStore(STORE_NAME).count());
    await transactionDone(transaction);
    return result;
  }

  async oldest(): Promise<PersistedCapture | undefined> {
    const database = await this.database();
    const transaction = database.transaction(STORE_NAME, "readonly");
    const request = transaction.objectStore(STORE_NAME).index(CREATED_AT_INDEX).openCursor();
    const cursor = await requestResult(request);
    await transactionDone(transaction);
    return cursor?.value as PersistedCapture | undefined;
  }

  async put(capture: PersistedCapture): Promise<void> {
    const database = await this.database();
    const transaction = database.transaction(STORE_NAME, "readwrite");
    transaction.objectStore(STORE_NAME).put(capture);
    await transactionDone(transaction);
  }

  async remove(idempotencyKey: string): Promise<void> {
    const database = await this.database();
    const transaction = database.transaction(STORE_NAME, "readwrite");
    transaction.objectStore(STORE_NAME).delete(idempotencyKey);
    await transactionDone(transaction);
  }

  private database(): Promise<IDBDatabase> {
    this.databasePromise ??= new Promise((resolve, reject) => {
      const request = this.factory.open(this.databaseName, DATABASE_VERSION);
      request.onupgradeneeded = () => {
        const database = request.result;
        const store = database.createObjectStore(STORE_NAME, {
          keyPath: "idempotencyKey",
        });
        store.createIndex(CREATED_AT_INDEX, ["createdAt", "idempotencyKey"], {
          unique: true,
        });
      };
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(
        request.error ?? new Error("The browser capture database could not be opened."),
      );
      request.onblocked = () => reject(
        new Error("The browser capture database upgrade was blocked."),
      );
    });
    return this.databasePromise;
  }
}
