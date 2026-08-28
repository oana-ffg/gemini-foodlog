import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import PrototypeDataNotice, { PROTOTYPE_DATA_NOTICE_VERSION } from "./PrototypeDataNotice";

describe("prototype data notice", () => {
  it("discloses the complete prototype processing boundary without promising zero retention", () => {
    const html = renderToStaticMarkup(<PrototypeDataNotice />);

    expect(PROTOTYPE_DATA_NOTICE_VERSION).toBe("prototype-data-use-v1");
    expect(html).toContain(`data-policy-version="${PROTOTYPE_DATA_NOTICE_VERSION}"`);
    expect(html).toContain("kitchen images, purchase emails, answers, inferences");
    expect(html).toContain("retained until a fixed retention and deletion policy is added");
    expect(html).toContain("Gemini processes relevant account data through Vertex AI");
    expect(html).toContain("does not use this customer data to train its AI models without permission");
    expect(html).toContain("Limited provider retention may still occur");
    expect(html).toContain("has not enabled optional Vertex request-and-response logging");
    expect(html).toContain("Oana may inspect narrowly scoped account evidence");
    expect(html).toContain("not to train a provider&#x27;s general models");
    expect(html).toContain("Antigravity will not receive private account data");
    expect(html).not.toContain("zero retention");
  });
});
