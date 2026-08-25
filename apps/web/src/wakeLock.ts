export type CaptureWakeLockStatus =
  | "inactive"
  | "requesting"
  | "active"
  | "hidden"
  | "unsupported"
  | "denied"
  | "released";

export interface WakeLockSentinelLike {
  readonly released: boolean;
  release(): Promise<void>;
  addEventListener(type: "release", listener: () => void): void;
}

export interface ScreenWakeLockProvider {
  request(type: "screen"): Promise<WakeLockSentinelLike>;
}

export interface VisibilityDocument {
  readonly visibilityState: DocumentVisibilityState;
  addEventListener(type: "visibilitychange", listener: () => void): void;
  removeEventListener(type: "visibilitychange", listener: () => void): void;
}

export class CaptureWakeLockController {
  private requested = false;
  private disposed = false;
  private generation = 0;
  private sentinel: WakeLockSentinelLike | null = null;

  constructor(
    private readonly provider: ScreenWakeLockProvider | undefined,
    private readonly visibility: VisibilityDocument,
    private readonly onStatus: (status: CaptureWakeLockStatus) => void,
  ) {
    this.visibility.addEventListener("visibilitychange", this.visibilityChanged);
  }

  start(): void {
    if (this.disposed) return;
    this.requested = true;
    void this.acquire();
  }

  stop(): void {
    this.requested = false;
    this.generation += 1;
    const sentinel = this.sentinel;
    this.sentinel = null;
    if (sentinel && !sentinel.released) void sentinel.release();
    this.onStatus("inactive");
  }

  dispose(): void {
    if (this.disposed) return;
    this.requested = false;
    this.generation += 1;
    const sentinel = this.sentinel;
    this.sentinel = null;
    this.disposed = true;
    if (sentinel && !sentinel.released) void sentinel.release();
    this.visibility.removeEventListener("visibilitychange", this.visibilityChanged);
  }

  private readonly visibilityChanged = () => {
    if (!this.requested || this.disposed) return;
    if (this.visibility.visibilityState !== "visible") {
      this.generation += 1;
      const sentinel = this.sentinel;
      this.sentinel = null;
      if (sentinel && !sentinel.released) void sentinel.release();
      this.onStatus("hidden");
      return;
    }
    void this.acquire();
  };

  private async acquire(): Promise<void> {
    if (!this.requested || this.disposed) return;
    if (!this.provider) {
      this.onStatus("unsupported");
      return;
    }
    if (this.visibility.visibilityState !== "visible") {
      this.onStatus("hidden");
      return;
    }
    if (this.sentinel && !this.sentinel.released) {
      this.onStatus("active");
      return;
    }

    const generation = ++this.generation;
    this.onStatus("requesting");
    try {
      const sentinel = await this.provider.request("screen");
      if (
        generation !== this.generation
        || !this.requested
        || this.disposed
        || this.visibility.visibilityState !== "visible"
      ) {
        if (!sentinel.released) await sentinel.release();
        return;
      }
      this.sentinel = sentinel;
      sentinel.addEventListener("release", () => {
        if (this.sentinel !== sentinel) return;
        this.sentinel = null;
        this.onStatus(
          this.visibility.visibilityState === "visible" ? "released" : "hidden",
        );
      });
      this.onStatus("active");
    } catch {
      if (generation === this.generation && this.requested && !this.disposed) {
        this.onStatus("denied");
      }
    }
  }
}
