import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("./api", () => ({
  joinWaitlist: vi.fn(),
  recordLaunchMailConsent: vi.fn(),
  withdrawLaunchMailConsent: vi.fn(),
  withdrawWaitlist: vi.fn(),
}));
vi.mock("./auth", () => ({
  SessionControls: () => <div>Signed-in account controls</div>,
}));

import { CapacityWaitlist, LaunchMailConsentControls } from "./ConsentControls";
import type { ConsentPreferences } from "./api";

const basePreferences: ConsentPreferences = {
  launch_mail_opt_in: false,
  launch_mail_policy_version: "launch-interest-v1",
  launch_mail_updated_at: "2026-08-27T12:00:00Z",
  waitlist_status: "not_joined",
  waitlist_policy_version: null,
  waitlist_updated_at: null,
};

const onChanged = async () => undefined;

describe("consent and capacity surfaces", () => {
  it("shows an explicit opt-in action when launch consent is absent or declined", () => {
    const html = renderToStaticMarkup(
      <LaunchMailConsentControls preferences={basePreferences} onChanged={onChanged} />,
    );

    expect(html).toContain("You are not subscribed");
    expect(html).toContain("Notify me at launch");
    expect(html).not.toContain("Withdraw consent");
  });

  it("shows immediate withdrawal when launch consent is active", () => {
    const html = renderToStaticMarkup(
      <LaunchMailConsentControls
        preferences={{ ...basePreferences, launch_mail_opt_in: true }}
        onChanged={onChanged}
      />,
    );

    expect(html).toContain("You asked us to email you");
    expect(html).toContain("Withdraw consent");
  });

  it("renders distinct join, joined, and withdrawn capacity states", () => {
    const notJoined = renderToStaticMarkup(
      <CapacityWaitlist preferences={basePreferences} onChanged={onChanged} />,
    );
    const joined = renderToStaticMarkup(
      <CapacityWaitlist
        preferences={{ ...basePreferences, waitlist_status: "active" }}
        onChanged={onChanged}
      />,
    );
    const withdrawn = renderToStaticMarkup(
      <CapacityWaitlist
        preferences={{ ...basePreferences, waitlist_status: "withdrawn" }}
        onChanged={onChanged}
      />,
    );

    expect(notJoined).toContain("Join the waitlist");
    expect(joined).toContain("You are on the waitlist");
    expect(joined).toContain("Leave the waitlist");
    expect(withdrawn).toContain("You left the waitlist");
    expect(withdrawn).toContain("Join the waitlist");
  });
});
