import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import {
  getOrCreateInboundMailAddress,
  getPurchase,
  listPurchases,
  type InboundMailAddress,
  type PurchaseDetail,
  type PurchaseSummary,
} from "./api";
import { SessionControls } from "./auth";

const dkk = new Intl.NumberFormat(undefined, {
  style: "currency",
  currency: "DKK",
});

function formatOre(value: number): string {
  return dkk.format(value / 100);
}

export function PurchaseEvidence({ purchase }: { purchase: PurchaseDetail }) {
  return (
    <section className="purchase-detail" aria-labelledby="purchase-detail-title">
      <div className="section-heading">
        <div>
          <p className="section-kicker">Persisted purchase evidence</p>
          <h2 id="purchase-detail-title">{purchase.merchant}</h2>
        </div>
        <span>{purchase.revision_count} source revision{purchase.revision_count === 1 ? "" : "s"}</span>
      </div>
      <p className="fine-print">
        Purchase ID {purchase.id} · Updated {new Date(purchase.updated_at).toLocaleString()}
      </p>
      {purchase.reconciliation ? (
        <div className="purchase-reconciliation">
          <strong>
            {purchase.reconciliation.unresolved_item_count === 0
              ? "Order and final evidence reconciled"
              : `${purchase.reconciliation.unresolved_item_count} item changes remain unresolved`}
          </strong>
          <p>
            {purchase.reconciliation.item_count} compared items
            {purchase.reconciliation.has_unresolved_substitution_pairing
              ? " · possible substitutions are intentionally not guessed"
              : ""}
          </p>
        </div>
      ) : (
        <p className="empty-state">Only one side of the purchase evidence is available, so no reconciliation was invented.</p>
      )}
      <div className="purchase-documents">
        {purchase.documents.map((document) => (
          <article key={document.id} className="purchase-document">
            <div className="entry-meta">
              <strong>{document.kind === "final" ? "Final invoice" : "Order confirmation"}</strong>
              <span>Revision {document.revision_number}</span>
            </div>
            <p>
              {document.order_reference ? `Order ${document.order_reference}` : "No order reference"}
              {document.invoice_reference ? ` · Invoice ${document.invoice_reference}` : ""}
            </p>
            {document.normalization ? (
              <p className="fine-print">
                Parsed by {document.normalization.parser_version} · {document.normalization.item_count} items · {document.normalization.charge_count} charges
              </p>
            ) : (
              <p className="form-message">This document is retained but has not been normalized.</p>
            )}
            <div className="purchase-table-wrap">
              <table className="purchase-table">
                <thead><tr><th>Item</th><th>Qty</th><th>State</th><th>Total</th></tr></thead>
                <tbody>
                  {document.items.map((item) => (
                    <tr key={item.id}>
                      <td>{item.name}<small>{item.category ?? "Uncategorized"}</small></td>
                      <td>{item.quantity}{item.unit_description ? ` ${item.unit_description}` : ""}</td>
                      <td>{item.disposition}</td>
                      <td>{formatOre(item.line_total_ore)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {document.charges.length > 0 ? (
              <dl className="purchase-charges">
                {document.charges.map((charge) => (
                  <div key={charge.id}>
                    <dt>{charge.description}</dt>
                    <dd>{formatOre(charge.amount_ore)}</dd>
                  </div>
                ))}
              </dl>
            ) : null}
          </article>
        ))}
      </div>
    </section>
  );
}

interface ForwardingSetupProps {
  address: InboundMailAddress | undefined;
  purchaseCount: number;
  message: string;
  onCopy: () => void;
}

export function ForwardingSetup({
  address,
  purchaseCount,
  message,
  onCopy,
}: ForwardingSetupProps) {
  return (
    <section className="forwarding-setup" aria-labelledby="forwarding-setup-title">
      <div className="section-heading">
        <div>
          <p className="section-kicker">Optional one-time setup</p>
          <h2 id="forwarding-setup-title">Send Nemlig purchase mail automatically</h2>
        </div>
        <span className={`forwarding-state forwarding-state--${purchaseCount > 0 ? "received" : "waiting"}`}>
          {purchaseCount > 0 ? "Purchase evidence received" : "Awaiting first purchase email"}
        </span>
      </div>
      <p>
        Create a mail rule in the inbox that receives your Nemlig emails. Match Nemlig order
        confirmations and final invoices, then forward only those messages to this private address.
      </p>
      <div className="forwarding-address">
        <code>{address?.address ?? "Preparing your private forwarding address…"}</code>
        <button type="button" disabled={!address} onClick={onCopy}>Copy address</button>
      </div>
      <ol className="forwarding-steps">
        <li>Open rules or filters in the mailbox where Nemlig sends your receipts.</li>
        <li>Match Nemlig purchase messages—not all of your mail.</li>
        <li>Forward them to the exact private address above and save the rule.</li>
      </ol>
      <p className="fine-print">
        This is optional. Camera capture and the food journal keep working if you skip it. FoodLog
        accepts the message as untrusted evidence and only recognizes authenticated Nemlig purchase
        mail; unrelated forwarded mail does not become purchase data.
      </p>
      <p className="form-message" role="status">{message}</p>
    </section>
  );
}

export default function PurchasesPage() {
  const [purchases, setPurchases] = useState<PurchaseSummary[]>([]);
  const [selectedId, setSelectedId] = useState<string>();
  const [detail, setDetail] = useState<PurchaseDetail>();
  const [message, setMessage] = useState("Loading purchase evidence…");
  const [forwardingAddress, setForwardingAddress] = useState<InboundMailAddress>();
  const [forwardingMessage, setForwardingMessage] = useState(
    "Preparing your stable private forwarding address…",
  );

  useEffect(() => {
    let active = true;
    void getOrCreateInboundMailAddress().then(
      (value) => {
        if (!active) return;
        setForwardingAddress(value);
        setForwardingMessage(
          "The address is stable for this account. Copy it into your mailbox rule once.",
        );
      },
      (error: unknown) => {
        if (active) {
          setForwardingMessage(
            error instanceof Error
              ? error.message
              : "The private forwarding address is unavailable.",
          );
        }
      },
    );
    return () => { active = false; };
  }, []);

  useEffect(() => {
    let active = true;
    void listPurchases(50).then(
      (items) => {
        if (!active) return;
        setPurchases(items);
        setSelectedId(items[0]?.id);
        setMessage(items.length === 0 ? "No purchase emails have been normalized yet." : "");
      },
      (error: unknown) => {
        if (active) setMessage(error instanceof Error ? error.message : "Purchase evidence is unavailable.");
      },
    );
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (!selectedId) {
      setDetail(undefined);
      return;
    }
    let active = true;
    setDetail(undefined);
    setMessage("Loading complete purchase evidence…");
    void getPurchase(selectedId).then(
      (value) => {
        if (!active) return;
        setDetail(value);
        setMessage("");
      },
      (error: unknown) => {
        if (active) setMessage(error instanceof Error ? error.message : "Purchase evidence is unavailable.");
      },
    );
    return () => { active = false; };
  }, [selectedId]);

  const copyForwardingAddress = async () => {
    if (!forwardingAddress) return;
    try {
      await navigator.clipboard.writeText(forwardingAddress.address);
      setForwardingMessage("Private forwarding address copied.");
    } catch {
      setForwardingMessage("Clipboard access failed. Select and copy the address manually.");
    }
  };

  return (
    <main className="data-page">
      <header className="data-page__header">
        <div>
          <p className="eyebrow">What FoodLog knows you bought</p>
          <h1>Purchase evidence</h1>
          <p>Confirmations and final invoices stay distinct. Final delivery evidence wins when the two disagree.</p>
        </div>
        <div className="data-page__account">
          <SessionControls />
          <Link to="/">Back to journal</Link>
        </div>
      </header>
      <ForwardingSetup
        address={forwardingAddress}
        purchaseCount={purchases.length}
        message={forwardingMessage}
        onCopy={() => void copyForwardingAddress()}
      />
      <div className="purchase-workspace">
        <nav className="purchase-index" aria-label="Purchases">
          {purchases.map((purchase) => (
            <button
              key={purchase.id}
              type="button"
              className={selectedId === purchase.id ? "purchase-index__item purchase-index__item--selected" : "purchase-index__item"}
              onClick={() => setSelectedId(purchase.id)}
            >
              <strong>{purchase.merchant}</strong>
              <span>{new Date(purchase.updated_at).toLocaleDateString()} · {purchase.revision_count} revision{purchase.revision_count === 1 ? "" : "s"}</span>
            </button>
          ))}
        </nav>
        <div>
          {message ? <p className="empty-state" role="status">{message}</p> : null}
          {detail ? <PurchaseEvidence purchase={detail} /> : null}
        </div>
      </div>
    </main>
  );
}
