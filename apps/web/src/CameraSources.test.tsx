import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";

vi.mock("./api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./api")>()),
  createDeviceCamera: vi.fn(),
  listCameras: vi.fn(),
  revokeCamera: vi.fn(),
}));

import CameraSources from "./CameraSources";

describe("camera source management", () => {
  it("keeps browser and physical source creation on the protected camera surface", () => {
    const html = renderToStaticMarkup(
      <CameraSources
        account={{
          id: "account-1",
          owner_user_id: "owner-1",
          entitlement_mode: "trial",
          trial_image_limit: 200,
          accepted_image_count: 4,
        }}
        currentBrowserCameraId={undefined}
        onRegisterBrowser={vi.fn()}
        onCurrentBrowserRevoked={vi.fn()}
      />,
    );

    expect(html).toContain("Camera sources");
    expect(html).toContain("Use this phone or browser");
    expect(html).toContain("Add a physical camera");
    expect(html).toContain("4 images accepted");
    expect(html).not.toContain("flc_v1_");
  });
});
