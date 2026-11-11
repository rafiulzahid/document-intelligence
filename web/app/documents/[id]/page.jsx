"use client";
import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useState } from "react";
import { FieldBadge, SevBadge, StatusBadge } from "@/components/Badge";
import { BASE, api, money, when } from "@/lib/api";

const FIELDS = [
  ["invoice_number", "Invoice number"], ["invoice_date", "Invoice date"], ["supplier_name", "Supplier"],
  ["customer_name", "Customer"], ["subtotal", "Subtotal"], ["tax", "Tax"], ["total", "Total"], ["currency", "Currency"],
];

export default function DocumentPage({ params }) {
  const { id } = use(params);
  const [doc, setDoc] = useState(null);
  const [res, setRes] = useState(null);
  const [sel, setSel] = useState(0);
  const [view, setView] = useState("file");
  const [page, setPage] = useState(1);
  const [err, setErr] = useState(null);

  const load = useCallback(async () => {
    try {
      const [d, r] = await Promise.all([api(`/documents/${id}`), api(`/documents/${id}/results?include_text=true`)]);
      setDoc(d); setRes(r); setErr(null);
    } catch (e) { setErr(e.message); }
  }, [id]);
  useEffect(() => { load(); }, [load]);
  const running = doc && ["queued", "processing"].includes(doc.status);
  useEffect(() => {
    if (!running) return;
    const t = setInterval(load, 2000);
    return () => clearInterval(t);
  }, [running, load]);

  const reprocess = async () => {
    try { await api(`/documents/${id}/reprocess`, { method: "POST" }); load(); } catch (e) { setErr(e.message); }
  };

  if (err && !doc) return <div className="err">{err}</div>;
  if (!doc || !res) return <p className="muted">Loading…</p>;
  const invoices = res.invoices || [];
  const inv = invoices[Math.min(sel, Math.max(0, invoices.length - 1))];
  const canPreview = ["pdf", "image", "tiff"].includes(doc.detected_type) && doc.status !== "failed";
  const pageCount = Math.max(1, doc.page_count || 1);
  const sheetCount = (res.pages || []).filter((p) => p.kind === "sheet").length;
  const pagesText = (res.pages || []).map((p) => `── ${p.kind === "sheet" ? `sheet ${p.name}` : `page ${p.page_no}`} · ${p.method}${p.ocr_confidence ? ` · OCR confidence ${p.ocr_confidence}` : ""} ──\n${p.text}`).join("\n\n");

  return (
    <>
      <div className="row" style={{ alignItems: "center" }}>
        <div className="grow">
          <div className="small"><Link href="/">← all documents</Link></div>
          <h1>{doc.filename} <StatusBadge status={doc.status} /></h1>
          <p className="sub">
            #{doc.id} · {doc.detected_type || "unknown type"} ·{" "}
            {sheetCount ? `${sheetCount} sheet(s)` : `${doc.page_count} page(s)`}{doc.ocr_pages ? `, ${doc.ocr_pages} OCR` : ""} ·{" "}
            {invoices.length} invoice(s) · uploaded {when(doc.created_at)}
            {doc.timings?.total_s !== undefined && ` · processed in ${doc.timings.total_s}s`}
            {doc.extraction?.methods && ` · method ${Object.keys(doc.extraction.methods).join(", ")}`}
          </p>
        </div>
        <button onClick={reprocess} disabled={running}>Reprocess</button>
      </div>
      {doc.error_code && <div className="err"><b>{doc.error_code}</b>: {doc.error_message}</div>}
      {doc.warnings?.length > 0 && <div className="small muted" style={{ marginBottom: 10 }}>Ingestion notes: {doc.warnings.join(" · ")}</div>}
      {running && <div className="okmsg">Processing… {doc.progress.total ? `${doc.progress.done}/${doc.progress.total} pages` : ""}</div>}

      <div className="split">
        <div className="panel preview">
          <div className="tabs">
            <button className={view === "file" ? "on" : ""} onClick={() => setView("file")}>Original</button>
            <button className={view === "text" ? "on" : ""} onClick={() => setView("text")}>Extracted text</button>
            <a className="btn" href={`${BASE}/documents/${id}/file`} target="_blank" rel="noreferrer">Open file ↗</a>
          </div>
          {view === "file" && canPreview && (
            <>
              {pageCount > 1 && (
                <div className="row" style={{ alignItems: "center", marginBottom: 8 }}>
                  <button className="sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>‹ prev</button>
                  <span className="small muted">page {page} of {pageCount}</span>
                  <button className="sm" disabled={page >= pageCount} onClick={() => setPage(page + 1)}>next ›</button>
                </div>
              )}
              <img src={`${BASE}/documents/${id}/preview?page=${page}`} alt={`${doc.filename} page ${page}`} />
            </>
          )}
          {view === "file" && !canPreview && <pre>{pagesText || "No preview available."}</pre>}
          {view === "text" && <pre>{pagesText || "No text extracted."}</pre>}
        </div>

        <div>
          {invoices.length === 0 && !running && (
            <div className="panel muted">{doc.status === "failed" ? "Processing failed — see the error above. Fix the file or click Reprocess." : "No invoice was found in this document."}</div>
          )}
          {invoices.length > 1 && (
            <div className="panel" style={{ padding: 12 }}>
              {invoices.length <= 10 ? (
                <div className="tabs" style={{ margin: 0 }}>
                  {invoices.map((x, k) => (
                    <button key={x.id} className={k === sel ? "on" : ""} onClick={() => { setSel(k); if (x.source?.pages?.[0]) setPage(x.source.pages[0]); }}>
                      {x.fields.invoice_number || `#${k + 1}`} <StatusBadge status={x.status} />
                    </button>
                  ))}
                </div>
              ) : (
                <label className="small muted">Invoice {sel + 1} of {invoices.length}:{" "}
                  <select value={sel} onChange={(e) => { const k = Number(e.target.value); setSel(k); const pg = invoices[k]?.source?.pages?.[0]; if (pg) setPage(pg); }}>
                    {invoices.map((x, k) => <option key={x.id} value={k}>{x.fields.invoice_number || `#${k + 1}`} · {x.status}{x.issues.some((i) => i.severity === "error") ? " · errors" : ""}</option>)}
                  </select>
                </label>
              )}
            </div>
          )}
          {inv && <InvoiceEditor key={inv.id} inv={inv} onSaved={load} />}

          <div className="panel">
            <h2>Processing history</h2>
            <ul className="timeline">
              {doc.events.map((e, k) => (
                <li key={k}><span className="mono">{when(e.at)}</span> · <b>{e.stage}</b> {e.status}
                  {e.duration_ms ? ` · ${Math.round(e.duration_ms)} ms` : ""}{e.message ? ` — ${e.message.slice(0, 220)}` : ""}</li>
              ))}
            </ul>
          </div>
        </div>
      </div>
    </>
  );
}

function InvoiceEditor({ inv, onSaved }) {
  const initial = useMemo(() => Object.fromEntries(FIELDS.map(([k]) => [k, inv.fields[k] ?? ""])), [inv]);
  const [vals, setVals] = useState(initial);
  const [items, setItems] = useState(inv.line_items.map(({ description, quantity, unit_price, amount }) => ({ description, quantity, unit_price, amount })));
  const [itemsDirty, setItemsDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState(null);
  const [err, setErr] = useState(null);
  const dirty = FIELDS.map(([k]) => k).filter((k) => String(vals[k] ?? "") !== String(initial[k] ?? ""));

  const save = async () => {
    setSaving(true); setErr(null); setMsg(null);
    try {
      const body = { fields: Object.fromEntries(dirty.map((k) => [k, vals[k] === "" ? null : vals[k]])), corrected_by: "reviewer" };
      if (itemsDirty) body.line_items = items.map((li) => ({
        description: li.description || null,
        quantity: li.quantity === "" || li.quantity === null ? null : Number(li.quantity),
        unit_price: li.unit_price === "" || li.unit_price === null ? null : Number(li.unit_price),
        amount: li.amount === "" || li.amount === null ? null : Number(li.amount) }));
      const r = await api(`/invoices/${inv.id}`, { method: "PATCH", body: JSON.stringify(body) });
      setMsg(`Saved and re-validated: status is now "${r.status.replace("_", " ")}" with ${r.issues.length} issue(s).`);
      setItemsDirty(false);
      onSaved();
    } catch (e) { setErr(e.message); }
    setSaving(false);
  };
  const setItem = (k, f, v) => { setItems(items.map((x, i) => (i === k ? { ...x, [f]: v } : x))); setItemsDirty(true); };
  const sum = items.reduce((a, li) => a + (Number(li.amount) || 0), 0);

  return (
    <>
      <div className="panel">
        <div className="row" style={{ alignItems: "center" }}>
          <h2 className="grow" style={{ margin: 0 }}>Extracted fields <StatusBadge status={inv.status} /></h2>
          <span className="small muted">method: {inv.method}</span>
        </div>
        <p className="small muted" style={{ margin: "6px 0 12px" }}>
          <span className="badge fs-found">found</span> in the document · <span className="badge fs-uncertain">uncertain</span> check it ·{" "}
          <span className="badge fs-removed">removed</span> model value not in the document · <span className="badge fs-missing">missing</span> not on the document ·{" "}
          <span className="badge fs-corrected">corrected</span> by a person
        </p>
        <div className="fields">
          {FIELDS.map(([k, label]) => {
            const m = inv.field_meta[k] || {};
            return (
              <FieldRow key={k} label={label} value={vals[k]} meta={m} dirty={dirty.includes(k)}
                        onChange={(v) => setVals({ ...vals, [k]: v })} />
            );
          })}
        </div>
        <h3>Line items {items.length ? `(${items.length}, sum ${money(sum)})` : ""}</h3>
        <table>
          <thead><tr><th>Description</th><th className="num">Qty</th><th className="num">Unit price</th><th className="num">Amount</th><th></th></tr></thead>
          <tbody>
            {items.map((li, k) => (
              <tr key={k}>
                <td><input style={{ width: "100%" }} value={li.description ?? ""} onChange={(e) => setItem(k, "description", e.target.value)} /></td>
                <td className="num"><input style={{ width: 70, textAlign: "right" }} value={li.quantity ?? ""} onChange={(e) => setItem(k, "quantity", e.target.value)} /></td>
                <td className="num"><input style={{ width: 95, textAlign: "right" }} value={li.unit_price ?? ""} onChange={(e) => setItem(k, "unit_price", e.target.value)} /></td>
                <td className="num"><input style={{ width: 105, textAlign: "right" }} value={li.amount ?? ""} onChange={(e) => setItem(k, "amount", e.target.value)} /></td>
                <td><button className="sm danger" onClick={() => { setItems(items.filter((_, i) => i !== k)); setItemsDirty(true); }}>×</button></td>
              </tr>
            ))}
            {!items.length && <tr><td colSpan={5} className="muted">No line items.</td></tr>}
          </tbody>
        </table>
        <div style={{ marginTop: 8 }}>
          <button className="sm" onClick={() => { setItems([...items, { description: "", quantity: "", unit_price: "", amount: "" }]); setItemsDirty(true); }}>+ Add line</button>
        </div>
        <div style={{ marginTop: 14, display: "flex", gap: 10, alignItems: "center" }}>
          <button className="primary" disabled={saving || (!dirty.length && !itemsDirty)} onClick={save}>
            {saving ? "Saving…" : `Save corrections${dirty.length || itemsDirty ? ` (${dirty.length + (itemsDirty ? 1 : 0)})` : ""}`}
          </button>
          {(dirty.length > 0 || itemsDirty) && <button onClick={() => { setVals(initial); setItems(inv.line_items); setItemsDirty(false); }}>Undo</button>}
          <span className="small muted">Saving re-runs all validation rules and the duplicate check.</span>
        </div>
        {msg && <div className="okmsg">{msg}</div>}
        {err && <div className="err">{err}</div>}
        {inv.warnings?.length > 0 && <div className="small muted" style={{ marginTop: 10 }}>Extraction notes: {inv.warnings.join(" · ")}</div>}
      </div>

      <div className="panel">
        <h2>Validation ({inv.issues.length} issue{inv.issues.length === 1 ? "" : "s"})</h2>
        {inv.issues.length === 0 ? <div className="okmsg">All checks passed.</div> : (
          <ul className="issues">
            {inv.issues.map((x) => (
              <li key={x.id}><SevBadge severity={x.severity} /> <b className="mono">{x.rule}</b> — {x.message}
                {x.details?.duplicate_of_document && <> · <Link href={`/documents/${x.details.duplicate_of_document}`}>open original</Link></>}</li>
            ))}
          </ul>
        )}
        {inv.corrections.length > 0 && (
          <>
            <h3>Correction history</h3>
            <table>
              <thead><tr><th>When</th><th>Field</th><th>Old</th><th>New</th><th>By</th></tr></thead>
              <tbody>{inv.corrections.map((c, k) => (
                <tr key={k}><td className="small">{when(c.at)}</td><td className="mono">{c.field}</td><td>{c.old ?? "—"}</td><td>{c.new ?? "—"}</td><td>{c.by || "—"}</td></tr>
              ))}</tbody>
            </table>
          </>
        )}
      </div>
    </>
  );
}

function FieldRow({ label, value, meta, dirty, onChange }) {
  const conf = meta.confidence !== null && meta.confidence !== undefined ? ` · conf ${Number(meta.confidence).toFixed(2)}` : "";
  const hint = [meta.evidence && `evidence: “${meta.evidence}”`, meta.note].filter(Boolean).join(" · ");
  return (
    <>
      <div className="k">{label}</div>
      <input className={dirty ? "dirty" : ""} value={value ?? ""} placeholder="(empty)" onChange={(e) => onChange(e.target.value)} />
      <div style={{ whiteSpace: "nowrap" }}><FieldBadge status={meta.status} /><span className="small muted">{conf}</span></div>
      {hint && <div className="evidence">{hint}</div>}
    </>
  );
}
