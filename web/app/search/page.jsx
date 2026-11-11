"use client";
import Link from "next/link";
import { useState } from "react";
import { StatusBadge } from "@/components/Badge";
import { api, money } from "@/lib/api";

const EMPTY = { supplier: "", customer: "", invoice_number: "", date_from: "", date_to: "", min_total: "", max_total: "",
                currency: "", status: "", file_type: "" };

function highlight(text, q) {
  if (!text || !q) return text;
  const terms = q.split(/\s+/).filter((t) => t.length > 1).map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  if (!terms.length) return text;
  const parts = text.split(new RegExp(`(${terms.join("|")})`, "ig"));
  return parts.map((p, i) => (i % 2 ? <mark key={i}>{p}</mark> : p));
}

export default function SearchPage() {
  const [q, setQ] = useState("");
  const [mode, setMode] = useState("hybrid");
  const [f, setF] = useState(EMPTY);
  const [res, setRes] = useState(null);
  const [err, setErr] = useState(null);
  const [loading, setLoading] = useState(false);

  const run = async (e) => {
    e?.preventDefault();
    setLoading(true); setErr(null);
    const p = new URLSearchParams({ mode, limit: "50" });
    if (q.trim()) p.set("q", q.trim());
    Object.entries(f).forEach(([k, v]) => v !== "" && p.set(k, v));
    try { setRes(await api(`/search?${p}`)); } catch (e2) { setErr(e2.message); }
    setLoading(false);
  };
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });

  return (
    <>
      <h1>Search</h1>
      <p className="sub">Keyword (full-text + fuzzy names), semantic (embeddings, tolerant to OCR typos) or hybrid (both, fused). Leave the box empty to list invoices by filters only.</p>
      <form className="panel" onSubmit={run}>
        <div className="row" style={{ alignItems: "center" }}>
          <input className="grow" autoFocus value={q} onChange={(e) => setQ(e.target.value)}
                 placeholder='e.g. "Hanari Carnes", 10253, "marble chess table", stationery receipt' />
          <div className="seg">
            {["keyword", "semantic", "hybrid"].map((m) => (
              <button type="button" key={m} className={mode === m ? "on" : ""} onClick={() => setMode(m)}>{m}</button>
            ))}
          </div>
          <button className="primary" type="submit" disabled={loading}>{loading ? "Searching…" : "Search"}</button>
        </div>
        <div className="filters">
          <label>Supplier<input value={f.supplier} onChange={set("supplier")} /></label>
          <label>Customer<input value={f.customer} onChange={set("customer")} /></label>
          <label>Invoice no.<input value={f.invoice_number} onChange={set("invoice_number")} /></label>
          <label>Date from<input type="date" value={f.date_from} onChange={set("date_from")} /></label>
          <label>Date to<input type="date" value={f.date_to} onChange={set("date_to")} /></label>
          <label>Status<select value={f.status} onChange={set("status")}><option value="">any</option><option>valid</option><option>needs_review</option></select></label>
          <label>Min total<input type="number" step="0.01" value={f.min_total} onChange={set("min_total")} /></label>
          <label>Max total<input type="number" step="0.01" value={f.max_total} onChange={set("max_total")} /></label>
          <label>Currency<input value={f.currency} placeholder="USD" onChange={set("currency")} /></label>
          <label>File type<select value={f.file_type} onChange={set("file_type")}><option value="">any</option>
            {["pdf", "image", "tiff", "xlsx", "xls", "csv"].map((x) => <option key={x}>{x}</option>)}</select></label>
          <label>&nbsp;<button type="button" onClick={() => { setF(EMPTY); setQ(""); setRes(null); }}>Reset</button></label>
        </div>
      </form>
      {err && <div className="err">{err}</div>}
      {res && (
        <div className="panel">
          <h2>{res.count} result{res.count === 1 ? "" : "s"} <span className="small muted">· mode: {res.mode}</span></h2>
          {res.warning && <div className="small muted">{res.warning}</div>}
          <table>
            <thead><tr><th>Invoice</th><th>Supplier / customer</th><th>Date</th><th className="num">Total</th><th>Status</th><th>Match</th></tr></thead>
            <tbody>
              {res.results.map((r, k) => (
                <tr key={k}>
                  <td><Link href={`/documents/${r.document_id}`}>{r.invoice_number || "(no number)"}</Link>
                    <div className="small muted">{r.filename}</div></td>
                  <td>{r.supplier_name || <span className="muted">—</span>}<div className="small muted">{r.customer_name || ""}</div></td>
                  <td>{r.invoice_date || "—"}</td>
                  <td className="num">{money(r.total, r.currency)}</td>
                  <td>{r.status ? <StatusBadge status={r.status} /> : r.document_status ? <StatusBadge status={r.document_status} /> : "—"}</td>
                  <td style={{ maxWidth: 420 }}>
                    <span className="small muted">{r.match}{r.page ? ` p.${r.page}` : ""}{r.score !== null ? ` · score ${r.score}` : ""}</span>
                    {r.snippet && <div className="snip">{highlight(r.snippet, q)}</div>}
                  </td>
                </tr>
              ))}
              {!res.results.length && <tr><td colSpan={6} className="muted">Nothing found.</td></tr>}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
