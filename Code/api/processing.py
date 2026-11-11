"""The background job: process one stored document and save everything to the database.

    process_document(document_id)   # called by the RQ worker, a thread, or directly in tests

Steps: ingest (text layer / OCR / sheets, with progress) -> extract (tables, LLM, rules) -> validate ->
duplicate check against the whole database -> store pages, invoices, line items, issues, search chunks.
A failure never crashes the worker: the document gets status "failed" with an error code and can be
reprocessed with POST /api/documents/{id}/reprocess.
"""
from __future__ import annotations

import json
import time
import traceback
from typing import Optional

from sqlalchemy import delete, select, text

from docintel.extract.llm import LLMClient
from docintel.extract.schema import HEADER_FIELDS, ExtractedInvoice, FieldMeta, LineItem
from docintel.ingest import ingest_file
from docintel.pipeline import process_ingested
from docintel.validate import duplicate_keys, review_status, validate_invoice

from . import config, db, embeddings
from .models import (Chunk, Document, Invoice, InvoiceKey, LineItemRow, Page, ProcessingEvent,
                     ValidationIssue)

_llm: Optional[LLMClient] = None


def llm() -> LLMClient:
    global _llm
    if _llm is None:
        _llm = LLMClient()
    return _llm


def _f(x):
    return None if x is None else float(x)


# ------------------------------------------------------------------ conversions
def row_to_extracted(inv: Invoice) -> ExtractedInvoice:
    e = ExtractedInvoice(invoice_number=inv.invoice_number, invoice_date=inv.invoice_date,
                         supplier_name=inv.supplier_name, customer_name=inv.customer_name,
                         subtotal=_f(inv.subtotal), tax=_f(inv.tax), total=_f(inv.total), currency=inv.currency,
                         method=inv.method or "", source=inv.source or {})
    e.line_items = [LineItem(li.description, li.quantity, _f(li.unit_price), _f(li.amount)) for li in inv.line_items]
    e.field_meta = {k: FieldMeta(**v) for k, v in (inv.field_meta or {}).items()}
    return e


def invoice_summary_text(inv: Invoice) -> str:
    parts = [f"Invoice {inv.invoice_number or '?'}", f"date {inv.invoice_date or '?'}",
             f"supplier {inv.supplier_name or '?'}", f"customer {inv.customer_name or '?'}",
             f"total {inv.currency or ''} {_f(inv.total) if inv.total is not None else '?'}"]
    items = "; ".join(li.description for li in inv.line_items if li.description)
    return " · ".join(parts) + (f" · items: {items}" if items else "")


def event(s, doc_id: int, stage: str, status: str, message: str = None, ms: float = None):
    s.add(ProcessingEvent(document_id=doc_id, stage=stage, status=status, message=message,
                          duration_ms=None if ms is None else round(ms, 1)))


# ------------------------------------------------------------------ duplicates against the database
def _keys_for(e: ExtractedInvoice, sha: str) -> list[str]:
    return [f"sha:{sha}"] + duplicate_keys(e)


def find_db_duplicates(s, inv_row: Invoice, e: ExtractedInvoice, sha: str) -> list[dict]:
    keys = _keys_for(e, sha)
    rows = s.execute(select(InvoiceKey.key, Invoice.id, Invoice.document_id, Invoice.invoice_number, Document.filename)
                     .join(Invoice, InvoiceKey.invoice_id == Invoice.id).join(Document, Invoice.document_id == Document.id)
                     .where(InvoiceKey.key.in_(keys), Invoice.id < inv_row.id).order_by(Invoice.id)).all()
    # only earlier invoices count: the first upload is the original, later ones are its duplicates
    for key, other_id, other_doc, other_no, other_name in rows:
        kind = key.split(":", 1)[0]
        if kind == "sha" and other_doc == inv_row.document_id:
            continue          # other invoices of the same file
        if kind == "sum" and e.invoice_number and other_no:
            continue          # different invoice numbers may share party/date/total legitimately
        how = {"sha": "identical file", "num": "same invoice number and party",
               "numtot": "same invoice number and total", "sum": "same party, date and total"}[kind]
        return [{"rule": "DUPLICATE", "severity": "error", "field": "invoice_number",
                 "message": f"duplicate of invoice #{other_id} in '{other_name}' ({how})",
                 "duplicate_of_invoice": other_id, "duplicate_of_document": other_doc}]
    return []


def is_register(inv_row: Invoice) -> bool:
    """Rows of a ledger/register (a flat table with several invoices) are records about invoices, not copies of
    them: they are used for cross-verification and are not part of duplicate detection."""
    src = inv_row.source or {}
    return inv_row.method == "table_mapping" and bool(src.get("rows")) and len(inv_row.document.invoices) > 1


def apply_validation(s, inv_row: Invoice, sha: str, check_duplicates: bool = True) -> list[dict]:
    """(Re)validate one stored invoice: rules + database duplicates; replaces issues and keys."""
    e = row_to_extracted(inv_row)
    issues = validate_invoice(e) + (find_db_duplicates(s, inv_row, e, sha) if check_duplicates else [])
    s.execute(delete(ValidationIssue).where(ValidationIssue.invoice_id == inv_row.id))
    s.execute(delete(InvoiceKey).where(InvoiceKey.invoice_id == inv_row.id))
    for i in issues:
        extra = {k: v for k, v in i.items() if k not in ("rule", "severity", "field", "message")}
        s.add(ValidationIssue(invoice_id=inv_row.id, rule=i["rule"], severity=i["severity"], field=i.get("field"),
                              message=i["message"], details=extra or None))
    for k in (set(_keys_for(e, sha)) if check_duplicates else ()):
        s.add(InvoiceKey(invoice_id=inv_row.id, key=k[:600]))
    inv_row.status = review_status(issues)
    s.flush()
    return issues


def document_status(doc: Document) -> str:
    if not doc.invoices:
        return "no_invoice"
    return "needs_review" if any(i.status == "needs_review" for i in doc.invoices) else "valid"


# ------------------------------------------------------------------ search chunks
def index_chunks(s, doc: Document, chunks: list[Chunk]):
    tag = embeddings.model_tag()
    vecs = None
    if tag:
        try:
            vecs = embeddings.embed([c.text for c in chunks])
        except Exception as e:  # noqa: BLE001
            event(s, doc.id, "index", "failed", f"embedding failed ({type(e).__name__}: {e}); keyword search still works")
    for k, c in enumerate(chunks):
        if vecs:
            c.embedding = {"m": tag, "v": vecs[k]}
        s.add(c)
    s.flush()
    if vecs and db.state["pgvector"] and db.ensure_vector_table(len(vecs[0])):
        for c in chunks:
            s.execute(text("INSERT INTO chunk_embeddings (chunk_id, embedding) VALUES (:i, CAST(:v AS vector)) "
                           "ON CONFLICT (chunk_id) DO UPDATE SET embedding = EXCLUDED.embedding"),
                      {"i": c.id, "v": json.dumps(c.embedding["v"])})


def reindex_invoice_chunk(s, inv: Invoice):
    s.execute(delete(Chunk).where(Chunk.invoice_id == inv.id, Chunk.kind == "invoice"))
    s.flush()
    index_chunks(s, inv.document, [Chunk(document_id=inv.document_id, invoice_id=inv.id, kind="invoice",
                                         text=invoice_summary_text(inv))])


# ------------------------------------------------------------------ the job
def process_document(document_id: int) -> str:
    t0 = time.perf_counter()
    with db.session_scope() as s:
        doc = s.get(Document, document_id)
        if doc is None:
            return "missing"
        doc.status, doc.error_code, doc.error_message = "processing", None, None
        doc.progress_done, doc.progress_total = 0, 0
        doc.attempts = (doc.attempts or 0) + 1
        event(s, doc.id, "ingest", "started")
        path, sha = doc.stored_path, doc.sha256

    last = [0.0]

    def progress(done, total):
        if time.perf_counter() - last[0] < 1.0 and done < total:
            return
        last[0] = time.perf_counter()
        with db.session_scope() as s2:
            d = s2.get(Document, document_id)
            d.progress_done, d.progress_total = done, total

    try:
        res = ingest_file(path, ocr=config.settings.ocr, progress=progress)
        t1 = time.perf_counter()
        rec = process_ingested(res, llm()) if res.ok else None
        t2 = time.perf_counter()
        with db.session_scope() as s:
            doc = s.get(Document, document_id)
            # reprocessing: drop previous results
            s.execute(delete(Chunk).where(Chunk.document_id == doc.id))
            s.execute(delete(Invoice).where(Invoice.document_id == doc.id))
            s.execute(delete(Page).where(Page.document_id == doc.id))
            s.flush()
            s.expire(doc)
            doc.detected_type = res.detected_type
            doc.page_count, doc.ocr_pages = res.page_count, res.ocr_pages
            doc.warnings = res.warnings
            doc.progress_done = doc.progress_total = max(res.page_count, len(res.sheets), 1)
            if not res.ok:
                msg = (res.error_message or "").replace(path, doc.filename)   # never expose server paths
                doc.status, doc.error_code, doc.error_message = "failed", res.error_code, msg
                event(s, doc.id, "ingest", "failed", f"{res.error_code}: {msg}", (t1 - t0) * 1000)
                return "failed"
            event(s, doc.id, "ingest", "ok", f"{res.page_count} page(s), {res.ocr_pages} OCR, {len(res.sheets)} sheet(s)",
                  (t1 - t0) * 1000)
            info = rec["extraction"]
            event(s, doc.id, "extract", "ok", f"{len(rec['invoices'])} invoice(s) via {info.get('methods')}"
                  + (f"; LLM errors: {info['llm_errors'][:1]}" if info.get("llm_errors") else ""), (t2 - t1) * 1000)
            doc.extraction_info = info

            chunks = []
            for p in res.pages:
                s.add(Page(document_id=doc.id, page_no=p.page_no, kind="page", text=p.text, method=p.method,
                           ocr_confidence=p.ocr_confidence))
                if p.text.strip():
                    chunks.append(Chunk(document_id=doc.id, page_no=p.page_no, kind="page", text=p.text[:8000]))
            for k, sh in enumerate(res.sheets, 1):
                s.add(Page(document_id=doc.id, page_no=k, kind="sheet", name=sh.name, text=sh.text, method="sheet"))
                if sh.text.strip():
                    chunks.append(Chunk(document_id=doc.id, page_no=k, kind="sheet", text=f"[{sh.name}]\n{sh.text[:8000]}"))
            s.flush()

            register = sum(1 for it in rec["invoices"] if it["invoice"].get("method") == "table_mapping"
                           and (it["invoice"].get("source") or {}).get("rows")) > 1
            for item in rec["invoices"]:
                e = ExtractedInvoice.from_dict(item["invoice"])
                inv = Invoice(document_id=doc.id, idx=item["index"], method=e.method, source=e.source,
                              field_meta={k: vars(v) for k, v in e.field_meta.items()}, extraction_warnings=e.warnings,
                              **{f: getattr(e, f) for f in HEADER_FIELDS})
                inv.line_items = [LineItemRow(position=i, description=li.description, quantity=li.quantity,
                                              unit_price=li.unit_price, amount=li.amount) for i, li in enumerate(e.line_items)]
                s.add(inv)
                s.flush()
                apply_validation(s, inv, sha, check_duplicates=not register)
                chunks.append(Chunk(document_id=doc.id, invoice_id=inv.id, kind="invoice", text=invoice_summary_text(inv)))
            s.refresh(doc)
            t3 = time.perf_counter()
            event(s, doc.id, "validate", "ok", f"{sum(len(i.issues) for i in doc.invoices)} issue(s)", (t3 - t2) * 1000)
            index_chunks(s, doc, chunks)
            doc.status = document_status(doc)
            doc.timings = {"ingest_s": round(t1 - t0, 3), "extract_s": round(t2 - t1, 3),
                           "store_s": round(time.perf_counter() - t2, 3), "total_s": round(time.perf_counter() - t0, 3)}
            event(s, doc.id, "done", "ok", f"status {doc.status}", (time.perf_counter() - t0) * 1000)
            return doc.status
    except Exception as e:  # noqa: BLE001  (never leave a document stuck in "processing")
        with db.session_scope() as s:
            doc = s.get(Document, document_id)
            if doc is not None:
                doc.status, doc.error_code = "failed", "INTERNAL_ERROR"
                doc.error_message = f"{type(e).__name__}: {e}".replace(path, doc.filename)
                event(s, doc.id, "error", "failed", traceback.format_exc()[-1500:])
        return "failed"
