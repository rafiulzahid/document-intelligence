"use client";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { StatusBadge } from "@/components/Badge";
import { api, fmtBytes, uploadFiles, when } from "@/lib/api";

const ACCEPT = ".pdf,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp,.xlsx,.xlsm,.xls,.csv,.tsv";

export default function Home() {
  const [docs, setDocs] = useState([]);
  const [total, setTotal] = useState(0);
  const [stats, setStats] = useState(null);
  const [files, setFiles] = useState([]);
  const [over, setOver] = useState(false);
  const [pct, setPct] = useState(null);
  const [msg, setMsg] = useState(null);
  const [err, setErr] = useState(null);
  const [filter, setFilter] = useState("");
  const input = useRef(null);

  const load = useCallback(async () => {
    try {
      const q = filter ? `?status=${filter}&limit=200` : "?limit=200";
      const [d, s] = await Promise.all([api(`/documents${q}`), api("/stats")]);
      setDocs(d.items); setTotal(d.total); setStats(s); setErr(null);
    } catch (e) { setErr(e.message); }
  }, [filter]);

  useEffect(() => { load(); }, [load]);
  const busy = docs.some((d) => d.status === "queued" || d.status === "processing");
  useEffect(() => {                                   // poll while something is running
    const t = setInterval(load, busy ? 2000 : 10000);
    return () => clearInterval(t);
  }, [busy, load]);

  const pick = (list) => setFiles(Array.from(list || []));
  const doUpload = async () => {
    if (!files.length) return;
    setPct(0); setMsg(null); setErr(null);
    try {
      const r = await uploadFiles(files, setPct);
      const rej = r.rejected?.length ? ` · rejected: ${r.rejected.map((x) => `${x.filename} (${x.error})`).join(", ")}` : "";
      setMsg(`Uploaded ${r.documents.length} file(s); processing in the background${rej}`);
      setFiles([]);
      load();
    } catch (e) { setErr(e.message); }
    setPct(null);
  };
  const reprocess = async (id) => {
    try { await api(`/documents/${id}/reprocess`, { method: "POST" }); load(); } catch (e) { setErr(e.message); }
  };
  const remove = async (id, name) => {
    if (!confirm(`Delete ${name} and its results?`)) return;
    try { await api(`/documents/${id}`, { method: "DELETE" }); load(); } catch (e) { setErr(e.message); }
  };

  const s = stats?.documents_by_status || {};
  return (
    <>
      <h1>Upload & processing</h1>
      <p className="sub">PDF (digital or scanned), JPG / PNG / TIFF, XLSX / XLS / CSV. Files are processed in the background.</p>

      <div className="stats">
        <div className="stat"><div className="n">{stats?.documents ?? "–"}</div><div className="l">documents</div></div>
        <div className="stat"><div className="n">{stats?.invoices ?? "–"}</div><div className="l">invoices</div></div>
        <div className="stat"><div className="n" style={{ color: "var(--ok)" }}>{s.valid ?? 0}</div><div className="l">valid</div></div>
        <div className="stat"><div className="n" style={{ color: "var(--warn)" }}>{s.needs_review ?? 0}</div><div className="l">needs review</div></div>
        <div className="stat"><div className="n" style={{ color: "var(--bad)" }}>{s.failed ?? 0}</div><div className="l">failed</div></div>
      </div>

      <div className="panel">
        <div className={`drop ${over ? "over" : ""}`} onClick={() => input.current?.click()}
             onDragOver={(e) => { e.preventDefault(); setOver(true); }} onDragLeave={() => setOver(false)}
             onDrop={(e) => { e.preventDefault(); setOver(false); pick(e.dataTransfer.files); }}>
          <b>Drop files here</b> or click to choose · several at once
          <input ref={input} type="file" multiple accept={ACCEPT} hidden onChange={(e) => pick(e.target.files)} />
          {files.length > 0 && (
            <ul className="filelist">{files.map((f) => <li key={f.name}>{f.name} · {fmtBytes(f.size)}</li>)}</ul>
          )}
        </div>
        <div style={{ marginTop: 12, display: "flex", gap: 10, alignItems: "center" }}>
          <button className="primary" disabled={!files.length || pct !== null} onClick={doUpload}>
            Upload {files.length ? `${files.length} file(s)` : ""}
          </button>
          {files.length > 0 && pct === null && <button onClick={() => setFiles([])}>Clear</button>}
          {pct !== null && <><div className="bar"><div style={{ width: `${pct}%` }} /></div><span className="small muted">{pct}%</span></>}
        </div>
        {msg && <div className="okmsg">{msg}</div>}
        {err && <div className="err">{err}</div>}
      </div>

      <div className="panel">
        <div className="row" style={{ alignItems: "center", marginBottom: 10 }}>
          <h2 className="grow" style={{ margin: 0 }}>Documents ({total}) {busy && <span className="small muted"> · updating…</span>}</h2>
          <select value={filter} onChange={(e) => setFilter(e.target.value)}>
            <option value="">all statuses</option>
            {["queued", "processing", "valid", "needs_review", "no_invoice", "failed"].map((x) => <option key={x}>{x}</option>)}
          </select>
          <button onClick={load}>Refresh</button>
        </div>
        <table>
          <thead><tr><th>#</th><th>File</th><th>Type</th><th>Status</th><th>Progress</th><th className="num">Pages</th>
            <th className="num">Invoices</th><th>Uploaded</th><th></th></tr></thead>
          <tbody>
            {docs.map((d) => (
              <tr key={d.id}>
                <td className="muted">{d.id}</td>
                <td><Link href={`/documents/${d.id}`}>{d.filename}</Link>
                  {d.error_code && <div className="small" style={{ color: "var(--bad)" }}>{d.error_code}: {d.error_message}</div>}</td>
                <td className="mono">{d.detected_type || "—"}</td>
                <td><StatusBadge status={d.status} /></td>
                <td>{["queued", "processing"].includes(d.status)
                  ? <><div className="bar"><div style={{ width: `${d.progress.percent}%` }} /></div>
                      <span className="small muted">{d.progress.total ? `${d.progress.done}/${d.progress.total} pages` : d.status}</span></>
                  : <span className="small muted">done</span>}</td>
                <td className="num">{d.page_count}{d.ocr_pages ? <span className="small muted"> ({d.ocr_pages} OCR)</span> : ""}</td>
                <td className="num">{d.invoice_count}</td>
                <td className="small muted">{when(d.created_at)}</td>
                <td className="right" style={{ whiteSpace: "nowrap" }}>
                  <button className="sm" disabled={d.status === "processing"} onClick={() => reprocess(d.id)}>Reprocess</button>{" "}
                  <button className="sm danger" onClick={() => remove(d.id, d.filename)}>Delete</button>
                </td>
              </tr>
            ))}
            {!docs.length && <tr><td colSpan={9} className="muted">No documents yet. Upload some files above.</td></tr>}
          </tbody>
        </table>
      </div>
    </>
  );
}
