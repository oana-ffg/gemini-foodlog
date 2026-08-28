export const PROTOTYPE_DATA_NOTICE_VERSION = "prototype-data-use-v1";

export default function PrototypeDataNotice() {
  return (
    <details
      className="prototype-data-notice"
      data-policy-version={PROTOTYPE_DATA_NOTICE_VERSION}
      open
    >
      <summary>How this prototype uses your data</summary>
      <div className="prototype-data-notice__body">
        <p>
          FoodLog stores the kitchen images, purchase emails, answers, inferences,
          and AI traces needed to build your private food journal. Prototype account
          data is retained until a fixed retention and deletion policy is added.
        </p>
        <p>
          Gemini processes relevant account data through Vertex AI. Google does not
          use this customer data to train its AI models without permission. Limited
          provider retention may still occur for service operation and abuse monitoring;
          FoodLog has not enabled optional Vertex request-and-response logging or data sharing.
        </p>
        <p>
          During debugging, Oana may inspect narrowly scoped account evidence with audited
          operator tools and coding agents such as Codex. This is used to improve FoodLog,
          not to train a provider&apos;s general models. Antigravity will not receive private
          account data unless its data controls are independently verified first.
        </p>
      </div>
    </details>
  );
}
