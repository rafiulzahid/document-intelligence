"use client";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, money } from "@/lib/api";

export default function ReviewPage() {
  const [rows, setRows] = useState(null);
  const [err, setErr] = useState(null);
  const load = useCallback(async () => {
    try { setRows((await api("/search?status=needs_review&limit=200")).results); setErr(null); } catch (e) { setErr(e.message); }
  }, []);
  useEffect(() => { load(); }, [load]);

  return (
    <>
      <h1>Review queue</h1>
      <p className="sub">Invoices with validation errors or warnings (missing fields, math that does not add up, duplicates,
        uncertain or discarded values). Open one, correct the values, and save: it is re-validated immediately.</p>
      {err && <div className="err">{err}</div>}
      <div className="panel">
        <div className="row" style={{ alignItems: "center", marginBottom: 10 }}>
          <h2 className="grow" style={{ margin: 0 }}>{rows ? `${rows.length} invoice(s) need review` : "Loading…"}</h2>
          <button onClick={load}>Refresh</button>
        </div>
        <table>
          <thead><tr><th>Invoice</th><th>File</th><th>Supplier</th><th>Customer</th><th>Date</th><th className="num">Total</th><th className="num">Issues</th></tr></thead>
          <tbody>
            {(rows || []).map((r) => (
              <tr key={r.invoice_id}>
                <td><Link href={`/documents/${r.document_id}`}>{r.invoice_number || "(no number)"}</Link></td>
                <td className="small">{r.filename}</td>
                <td>{r.supplier_name || <span className="muted">—</span>}</td>
                <td>{r.customer_name || <span className="muted">—</span>}</td>
                <td>{r.invoice_date || "—"}</td>
                <td className="num">{money(r.total, r.currency)}</td>
                <td className="num">{r.issue_count}</td>
              </tr>
            ))}
            {rows && !rows.length && <tr><td colSpan={7} className="muted">Nothing to review.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
