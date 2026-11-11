"""FastAPI application.

Run locally (no Docker):   cd Code && uvicorn api.main:app --reload --port 8000
Interactive docs:          http://localhost:8000/docs
"""
from __future__ import annotations

import io
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docintel.crossverify import cross_verify
from docintel.normalize import normalize_currency, parse_amount, parse_date

from . import config, db, jobqueue
from .models import Correction, Document, Invoice, LineItemRow
from .processing import (apply_validation, document_status, event, is_register, reindex_invoice_chunk,
                         row_to_extracted)
from .search import search as run_search
from .serializers import document_brief, document_detail, invoice_detail
from .storage import safe_name, save_upload

@asynccontextmanager
async def lifespan(_app):
    db.init_db()
    yield


app = FastAPI(title="Document Intelligence API", version="1.0.0", lifespan=lifespan,
              description="Upload invoices (PDF, scans, images, XLSX/XLS/CSV) -> extracted, validated, searchable data.")
app.add_middleware(CORSMiddleware, allow_origins=config.settings.cors_origins, allow_credentials=False,
                   allow_methods=["GET", "POST", "PATCH", "DELETE"], allow_headers=["*"])


def require_key(x_api_key: Optional[str] = Header(default=None)):
    if config.settings.api_key and x_api_key != config.settings.api_key:
        raise HTTPException(401, "Missing or invalid X-API-Key header")


Auth = Depends(require_key)


def _doc_or_404(s: Session, doc_id: int) -> Document:
    d = s.get(Document, doc_id)
    if d is None:
        raise HTTPException(404, f"Document {doc_id} not found")
    return d


# ------------------------------------------------------------------ health
@app.get("/api/health")
def health(s: Session = Depends(db.get_session)):
    s.execute(select(1))
    return {"status": "ok", "database": "postgresql" if config.settings.is_postgres else "sqlite",
            "pgvector": db.state["pgvector"], "pg_trgm": db.state["pg_trgm"], "queue": jobqueue.queue_info(),
            "embeddings": config.settings.embed_provider, "auth": bool(config.settings.api_key)}


@app.get("/api/stats", dependencies=[Auth])
def stats(s: Session = Depends(db.get_session)):
    by_status = dict(s.execute(select(Document.status, func.count()).group_by(Document.status)).all())
    inv_status = dict(s.execute(select(Invoice.status, func.count()).group_by(Invoice.status)).all())
    return {"documents": sum(by_status.values()), "documents_by_status": by_status,
            "invoices": sum(inv_status.values()), "invoices_by_status": inv_status}


# ------------------------------------------------------------------ documents
@app.post("/api/documents", status_code=201, dependencies=[Auth])
async def upload(files: list[UploadFile] = File(...), s: Session = Depends(db.get_session)):
    """Upload one or more files. Each is stored and queued for background processing."""
    if not files:
        raise HTTPException(400, "No files uploaded")
    created, rejected = [], []
    for f in files:
        try:
            path, size, sha = await save_upload(f)
        except HTTPException as e:
            rejected.append({"filename": f.filename, "error": e.detail, "status_code": e.status_code})
            continue
        d = Document(filename=safe_name(f.filename), stored_path=str(path), content_type=f.content_type,
                     size_bytes=size, sha256=sha, status="queued")
        s.add(d)
        s.flush()
        event(s, d.id, "queued", "ok", f"{size} bytes")
        s.commit()
        created.append(d.id)
    for doc_id in created:
        jobqueue.enqueue(doc_id)
    docs = [document_brief(s.get(Document, i)) for i in created]
    if not docs and rejected:
        raise HTTPException(415 if all(r["status_code"] == 415 for r in rejected) else 400,
                            {"message": "No file accepted", "rejected": rejected})
    return {"documents": docs, "rejected": rejected}


@app.get("/api/documents", dependencies=[Auth])
def list_documents(status: Optional[str] = None, file_type: Optional[str] = None, q: Optional[str] = None,
                   limit: int = Query(50, le=500), offset: int = 0, s: Session = Depends(db.get_session)):
    query = select(Document)
    if status:
        query = query.where(Document.status == status)
    if file_type:
        query = query.where(Document.detected_type == file_type)
    if q:
        query = query.where(Document.filename.ilike(f"%{q}%"))
    total = s.execute(select(func.count()).select_from(query.subquery())).scalar()
    rows = s.execute(query.order_by(Document.id.desc()).limit(limit).offset(offset)).scalars().all()
    return {"total": total, "items": [document_brief(d) for d in rows]}


@app.get("/api/documents/{doc_id}", dependencies=[Auth])
def get_document(doc_id: int, s: Session = Depends(db.get_session)):
    return document_detail(_doc_or_404(s, doc_id))


@app.get("/api/documents/{doc_id}/results", dependencies=[Auth])
def get_results(doc_id: int, include_text: bool = False, s: Session = Depends(db.get_session)):
    d = _doc_or_404(s, doc_id)
    out = {"document": document_brief(d), "invoices": [invoice_detail(i) for i in d.invoices]}
    if include_text:
        out["pages"] = [{"page_no": p.page_no, "kind": p.kind, "name": p.name, "method": p.method,
                         "ocr_confidence": p.ocr_confidence, "text": p.text} for p in d.pages]
    return out


@app.get("/api/documents/{doc_id}/file", dependencies=[Auth])
def get_file(doc_id: int, s: Session = Depends(db.get_session)):
    d = _doc_or_404(s, doc_id)
    if not Path(d.stored_path).exists():
        raise HTTPException(410, "Stored file is no longer available")
    return FileResponse(d.stored_path, filename=d.filename, content_disposition_type="inline")


@app.get("/api/documents/{doc_id}/preview", dependencies=[Auth])
def preview(doc_id: int, page: int = Query(1, ge=1), dpi: int = Query(110, ge=50, le=200),
            s: Session = Depends(db.get_session)):
    """PNG image of one page (PDF page, TIFF frame or image), so every browser can show it."""
    d = _doc_or_404(s, doc_id)
    if d.detected_type not in ("pdf", "image", "tiff") or not Path(d.stored_path).exists():
        raise HTTPException(404, "No preview for this document type")
    buf = io.BytesIO()
    try:
        if d.detected_type == "pdf":
            import pymupdf
            with pymupdf.open(d.stored_path) as pdf:
                if page > pdf.page_count:
                    raise HTTPException(404, f"Page {page} does not exist ({pdf.page_count} pages)")
                buf.write(pdf[page - 1].get_pixmap(dpi=dpi).tobytes("png"))
        else:
            from PIL import Image, ImageSequence
            with Image.open(d.stored_path) as im:
                frames = list(ImageSequence.Iterator(im))
                if page > len(frames):
                    raise HTTPException(404, f"Page {page} does not exist ({len(frames)} pages)")
                fr = frames[page - 1].convert("RGB")
                fr.thumbnail((1700, 2400))
                fr.save(buf, "PNG")
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001  (e.g. a damaged file)
        raise HTTPException(422, f"Cannot render a preview: {e}") from e
    return Response(content=buf.getvalue(), media_type="image/png", headers={"cache-control": "private, max-age=300"})


@app.post("/api/documents/{doc_id}/reprocess", dependencies=[Auth])
def reprocess(doc_id: int, s: Session = Depends(db.get_session)):
    d = _doc_or_404(s, doc_id)
    if d.status == "processing":
        raise HTTPException(409, "Document is already being processed")
    d.status = "queued"
    event(s, d.id, "queued", "ok", "reprocess requested")
    s.commit()
    jobqueue.enqueue(d.id)
    s.refresh(d)
    return document_brief(d)


@app.delete("/api/documents/{doc_id}", status_code=204, dependencies=[Auth])
def delete_document(doc_id: int, s: Session = Depends(db.get_session)):
    d = _doc_or_404(s, doc_id)
    Path(d.stored_path).unlink(missing_ok=True)
    s.delete(d)
    s.commit()


# ------------------------------------------------------------------ invoices + corrections
@app.get("/api/invoices/{inv_id}", dependencies=[Auth])
def get_invoice(inv_id: int, s: Session = Depends(db.get_session)):
    i = s.get(Invoice, inv_id)
    if i is None:
        raise HTTPException(404, f"Invoice {inv_id} not found")
    return invoice_detail(i)


class LineItemIn(BaseModel):
    description: Optional[str] = None
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    amount: Optional[float] = None


class CorrectionIn(BaseModel):
    fields: dict[str, Any] = Field(default_factory=dict,
                                   description="e.g. {\"total\": 1444.8, \"supplier_name\": \"ACME\"}; null clears a value")
    line_items: Optional[list[LineItemIn]] = Field(default=None, description="replaces all line items when given")
    corrected_by: Optional[str] = None


EDITABLE = {"invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency"}


@app.patch("/api/invoices/{inv_id}", dependencies=[Auth])
def correct_invoice(inv_id: int, body: CorrectionIn, s: Session = Depends(db.get_session)):
    """Manual correction: values are normalised, saved with an audit trail, and the invoice is re-validated."""
    i = s.get(Invoice, inv_id)
    if i is None:
        raise HTTPException(404, f"Invoice {inv_id} not found")
    bad = set(body.fields) - EDITABLE
    if bad:
        raise HTTPException(422, f"Not editable: {sorted(bad)}. Editable: {sorted(EDITABLE)}")
    meta = dict(i.field_meta or {})
    for f, v in body.fields.items():
        if v in ("", None):
            new = None
        elif f in ("subtotal", "tax", "total"):
            new = parse_amount(v)
            if new is None:
                raise HTTPException(422, f"{f}: '{v}' is not a number")
        elif f == "invoice_date":
            new, _ = parse_date(v)
            if new is None:
                raise HTTPException(422, f"invoice_date: '{v}' is not a date")
        elif f == "currency":
            new, _ = normalize_currency(v)
            if new is None:
                raise HTTPException(422, f"currency: '{v}' is not a known currency")
        else:
            new = str(v).strip()
        old = getattr(i, f)
        old_s = None if old is None else str(float(old) if f in ("subtotal", "tax", "total") else old)
        new_s = None if new is None else str(new)
        if old_s == new_s:
            continue
        setattr(i, f, new)
        s.add(Correction(invoice_id=i.id, field=f, old_value=old_s, new_value=new_s, corrected_by=body.corrected_by))
        meta[f] = {"status": "corrected", "confidence": 1.0, "evidence": None, "raw": new_s,
                   "note": f"manually corrected (was {old_s})"}
    if body.line_items is not None:
        old_n = len(i.line_items)
        i.line_items = [LineItemRow(position=k, **li.model_dump()) for k, li in enumerate(body.line_items)]
        s.add(Correction(invoice_id=i.id, field="line_items", old_value=f"{old_n} items",
                         new_value=f"{len(body.line_items)} items", corrected_by=body.corrected_by))
    i.field_meta = meta
    s.flush()
    apply_validation(s, i, i.document.sha256, check_duplicates=not is_register(i))
    s.refresh(i)
    reindex_invoice_chunk(s, i)
    i.document.status = document_status(i.document)
    event(s, i.document_id, "correction", "ok", f"invoice {i.id}: {sorted(body.fields) + (['line_items'] if body.line_items is not None else [])}")
    s.commit()
    s.refresh(i)
    return invoice_detail(i)


# ------------------------------------------------------------------ search
@app.get("/api/search", dependencies=[Auth])
def search(q: Optional[str] = None, mode: str = Query("hybrid", pattern="^(keyword|semantic|hybrid)$"),
           supplier: Optional[str] = None, customer: Optional[str] = None, invoice_number: Optional[str] = None,
           date_from: Optional[date] = None, date_to: Optional[date] = None,
           min_total: Optional[float] = None, max_total: Optional[float] = None,
           currency: Optional[str] = None, status: Optional[str] = None, file_type: Optional[str] = None,
           limit: int = Query(20, le=200), s: Session = Depends(db.get_session)):
    filters = dict(supplier=supplier, customer=customer, invoice_number=invoice_number,
                   date_from=date_from.isoformat() if date_from else None, date_to=date_to.isoformat() if date_to else None,
                   min_total=min_total, max_total=max_total, currency=currency, status=status, file_type=file_type)
    return run_search(s, q, mode, filters, limit)


# ------------------------------------------------------------------ cross-verification
class CrossVerifyIn(BaseModel):
    left_document_ids: list[int]
    right_document_ids: list[int]
    left_name: str = "left"
    right_name: str = "right"


@app.post("/api/cross-verify", dependencies=[Auth])
def cross_verify_docs(body: CrossVerifyIn, s: Session = Depends(db.get_session)):
    """Compare invoices across documents (PDF <-> PDF, PDF <-> Excel ledger), matched by invoice number."""
    def load(ids):
        invs = []
        for i in ids:
            invs += [row_to_extracted(x) for x in _doc_or_404(s, i).invoices]
        return invs
    return cross_verify(load(body.left_document_ids), load(body.right_document_ids), body.left_name, body.right_name)
