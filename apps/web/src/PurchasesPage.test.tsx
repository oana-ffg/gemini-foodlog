import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import { PurchaseEvidence } from "./PurchasesPage";
import type { PurchaseDetail } from "./api";

const purchase: PurchaseDetail = {
  id: "purchase-1",
  merchant: "Nemlig",
  revision_count: 2,
  latest_confirmation_document_id: "document-confirmation",
  latest_final_document_id: "document-final",
  created_at: "2026-08-20T10:00:00Z",
  updated_at: "2026-08-21T10:00:00Z",
  documents: [
    {
      id: "document-final",
      kind: "final",
      revision_number: 2,
      order_reference: "ORDER-1",
      invoice_reference: "INVOICE-1",
      created_at: "2026-08-21T10:00:00Z",
      normalization: {
        parser_version: "nemlig-v1",
        item_count: 1,
        charge_count: 1,
        included_vat_ore: 400,
        created_at: "2026-08-21T10:01:00Z",
      },
      items: [
        {
          id: "item-1",
          ordinal: 1,
          name: "Duck breast",
          normalized_name: "duck breast",
          disposition: "delivered",
          quantity: 1,
          category: "Meat",
          unit_description: "pack",
          unit_price_ore: 12900,
          included_discount_ore: null,
          line_total_ore: 12900,
        },
      ],
      charges: [{ id: "charge-1", kind: "total", amount_ore: 12900, description: "Total" }],
    },
  ],
  reconciliation: {
    confirmation_document_id: "document-confirmation",
    final_document_id: "document-final",
    item_count: 1,
    unresolved_item_count: 0,
    has_unresolved_substitution_pairing: false,
    items: [],
    updated_at: "2026-08-21T10:02:00Z",
  },
};

describe("purchase evidence view", () => {
  it("keeps final evidence, normalization, items, charges, and reconciliation visible", () => {
    const html = renderToStaticMarkup(<PurchaseEvidence purchase={purchase} />);

    expect(html).toContain("Final invoice");
    expect(html).toContain("Order and final evidence reconciled");
    expect(html).toContain("Duck breast");
    expect(html).toContain("nemlig-v1");
    expect(html).toContain("Total");
    expect(html).not.toContain("pantry inventory");
  });

  it("does not invent reconciliation when only one document side exists", () => {
    const html = renderToStaticMarkup(
      <PurchaseEvidence purchase={{ ...purchase, reconciliation: null }} />,
    );

    expect(html).toContain("no reconciliation was invented");
  });
});
