"use client";
import { useEffect, useState } from "react";
import { api, money } from "@/lib/api";

function Picker({ title, docs, sel, setSel, filter, setFilter }) {
  const shown = docs.filter((d) => !filter || d.filename.toLowerCase().includes(filter.toLowerCase()));
  const toggle = (id) => setSel(sel.includes(id) ? sel.filter((x) => x !== id) : [...sel, id]);
  return (
    <div className="panel grow">
      <h2>{title} <span className="small muted">({sel.length} selected)</span></h2>
      <div className="row" style={{ marginBottom: 8 }}>
        <input className="grow" placeholder="filter by file name, e.g. .pdf or ledger" value={filter} onChange={(e) => setFilter(e.target.value)} />
        <button type="button" onClick={() => setSel([...new Set([...sel, ...shown.map((d) => d.id)])])}>Select shown</button>
        <button type="button" onClick={() => setSel([])}>None</button>
      </div>
      <div className="checklist">
        {shown.map((d) => (
          <label key={d.id}><input type="checkbox" checked={sel.includes(d.id)} onChange={() => toggle(d.id)} /> #{d.id} {d.filename}
            <span className="small muted"> · {d.invoice_count} invoice(s)</span></label>
        ))}
        {!shown.length && <div className="muted small">No processed documents match.</div>}
      </div>
    </div>
  );
}

export default function CrossVerifyPage() {
  const [docs, setDocs] = useState([]);
  const [left, setLeft] = useState([]);
  const [right, setRight] = useState([]);
  const [lf, setLf] = useState(".pdf");
  const [rf, setRf] = useState(".xls");
  const [res, setRes] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => {
    api("/documents?limit=500").then((d) => setDocs(d.items.filter((x) => x.invoice_count > 0))).catch((e) => setErr(e.message));
  }, []);
  const run = async () => {
    setErr(null); setRes(null);
    try {
      setRes(await api("/cross-verify", { method: "POST", body: JSON.stringify({
        left_document_ids: left, right_document_ids: right, left_name: "documents", right_name: "ledger" }) }));
    } catch (e) { setErr(e.message); }
  };
  const fmt = (field, v) => (v === null || v === undefined ? "—" : /total|subtotal|tax|amount|price/.test(field) ? money(v) : String(v));

  return (
    <>
      <h1>Cross-verification</h1>
      <p className="sub">Compare invoices across documents — e.g. invoice PDFs against an Excel/CSV ledger. Invoices are matched by
        invoice number; header fields and line items are compared.</p>
      <div className="row">
        <Picker title="Documents (e.g. PDFs)" docs={docs} sel={left} setSel={setLeft} filter={lf} setFilter={setLf} />
        <Picker title="Compare against (e.g. ledger)" docs={docs} sel={right} setSel={setRight} filter={rf} setFilter={setRf} />
      </div>
      <button className="primary" disabled={!left.length || !right.length} onClick={run}>Compare</button>
      {err && <div className="err">{err}</div>}
      {res && (
        <div className="panel" style={{ marginTop: 18 }}>
          <h2>{res.consistent ? "Consistent ✓" : `${res.mismatches.length} mismatch(es)`} <span className="small muted">· {res.matched} invoice(s) matched</span></h2>
          {res.mismatches.length > 0 && (
            <table>
              <thead><tr><th>Invoice</th><th>Field</th><th>Item</th><th className="num">Documents</th><th className="num">Ledger</th></tr></thead>
              <tbody>{res.mismatches.map((m, k) => (
                <tr key={k}><td className="mono">{m.invoice_number}</td><td className="mono">{m.field}</td><td>{m.description || ""}</td>
                  <td className="num">{fmt(m.field, m.documents)}</td><td className="num">{fmt(m.field, m.ledger)}</td></tr>
              ))}</tbody>
            </table>
          )}
          <h3>Only in the documents ({res.only_in_documents.length})</h3>
          <div className="small mono">{res.only_in_documents.join(", ") || "—"}</div>
          <h3>Only in the ledger ({res.only_in_ledger.length})</h3>
          <div className="small mono">{res.only_in_ledger.join(", ") || "—"}</div>
        </div>
      )}
    </>
  );
}
