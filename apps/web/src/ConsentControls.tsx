import { useState } from "react";
import {
  joinWaitlist,
  recordLaunchMailConsent,
  withdrawLaunchMailConsent,
  withdrawWaitlist,
  type ConsentPreferences,
} from "./api";
import { SessionControls } from "./auth";

interface ConsentControlsProps {
  preferences: ConsentPreferences;
  onChanged: () => Promise<void>;
}

function safeMessage(error: unknown, fallback: string): string {
  return error instanceof Error ? error.message : fallback;
}

export function LaunchMailConsentControls({
  preferences,
  onChanged,
}: ConsentControlsProps) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string>();
  const optedIn = preferences.launch_mail_opt_in === true;

  const update = async () => {
    setBusy(true);
    setMessage(optedIn ? "Withdrawing…" : "Saving…");
    try {
      if (optedIn) {
        await withdrawLaunchMailConsent();
      } else {
        await recordLaunchMailConsent(true);
      }
      await onChanged();
      setMessage(optedIn ? "You will not receive launch mail." : "Launch notification enabled.");
    } catch (error: unknown) {
      setMessage(safeMessage(error, "Could not update launch notifications."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="consent-card" aria-labelledby="launch-mail-title">
      <div>
        <p className="section-kicker">Product updates</p>
        <h2 id="launch-mail-title">Full-product launch notification</h2>
        <p>
          {optedIn
            ? "You asked us to email you when FoodLog becomes a full product."
            : "You are not subscribed to the FoodLog launch mailing list."}
        </p>
      </div>
      <div className="consent-card__action">
        <button type="button" className={optedIn ? "button--quiet" : undefined} onClick={update} disabled={busy}>
          {busy ? "Saving…" : optedIn ? "Withdraw consent" : "Notify me at launch"}
        </button>
        {message ? <p className="form-message" role="status">{message}</p> : null}
      </div>
    </section>
  );
}

export function CapacityWaitlist({
  preferences,
  onChanged,
}: ConsentControlsProps) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string>();
  const joined = preferences.waitlist_status === "active";
  const fulfilled = preferences.waitlist_status === "fulfilled";

  const update = async () => {
    if (fulfilled) {
      await onChanged();
      return;
    }
    setBusy(true);
    setMessage(joined ? "Withdrawing…" : "Joining…");
    try {
      if (joined) {
        await withdrawWaitlist();
      } else {
        await joinWaitlist();
      }
      await onChanged();
      setMessage(joined ? "You have left the waitlist." : "You are on the waitlist.");
    } catch (error: unknown) {
      setMessage(safeMessage(error, "Could not update the waitlist."));
    } finally {
      setBusy(false);
    }
  };

  return (
    <main className="auth-shell">
      <section className="auth-card capacity-card" aria-labelledby="capacity-title">
        <SessionControls />
        <p className="eyebrow">FoodLog early access</p>
        <h1 id="capacity-title">The 25 trial spots are full.</h1>
        <p>
          {joined
            ? "You are on the waitlist. We will email you about FoodLog access for this purpose only."
            : preferences.waitlist_status === "fulfilled"
              ? "Your early-access account is active."
              : preferences.waitlist_status === "withdrawn"
              ? "You left the waitlist. You can join again while early access remains full."
              : "Join the waitlist if you want an email when another FoodLog spot becomes available."}
        </p>
        <button type="button" className={joined ? "button--quiet" : undefined} onClick={update} disabled={busy || fulfilled}>
          {busy ? "Saving…" : fulfilled ? "Account active" : joined ? "Leave the waitlist" : "Join the waitlist"}
        </button>
        {message ? <p className="form-message" role="status">{message}</p> : null}
      </section>
    </main>
  );
}
