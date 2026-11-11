"""Database rows -> JSON for the API."""
from __future__ import annotations

from .models import Document, Invoice


def _f(x):
    return None if x is None else float(x)


def _dt(x):
    return x.isoformat() if x else None


def document_brief(d: Document) -> dict:
    total = d.progress_total or 0
    return {"id": d.id, "filename": d.filename, "status": d.status, "detected_type": d.detected_type,
            "size_bytes": d.size_bytes, "page_count": d.page_count, "ocr_pages": d.ocr_pages,
            "progress": {"done": d.progress_done, "total": total,
                         "percent": 100 if d.status not in ("queued", "processing") else
                         (round(100 * d.progress_done / total) if total else 0)},
            "invoice_count": len(d.invoices), "error_code": d.error_code, "error_message": d.error_message,
            "attempts": d.attempts, "created_at": _dt(d.created_at), "updated_at": _dt(d.updated_at)}


def document_detail(d: Document) -> dict:
    out = document_brief(d)
    out.update(sha256=d.sha256, warnings=d.warnings or [], timings=d.timings, extraction=d.extraction_info,
               events=[{"stage": e.stage, "status": e.status, "message": e.message, "duration_ms": e.duration_ms,
                        "at": _dt(e.created_at)} for e in d.events])
    return out


def invoice_brief(i: Invoice) -> dict:
    return {"invoice_id": i.id, "document_id": i.document_id, "filename": i.document.filename if i.document else None,
            "invoice_number": i.invoice_number, "invoice_date": i.invoice_date, "supplier_name": i.supplier_name,
            "customer_name": i.customer_name, "total": _f(i.total), "currency": i.currency, "status": i.status,
            "issue_count": len(i.issues)}


def invoice_detail(i: Invoice) -> dict:
    return {"id": i.id, "document_id": i.document_id, "index": i.idx, "status": i.status, "method": i.method,
            "fields": {"invoice_number": i.invoice_number, "invoice_date": i.invoice_date,
                       "supplier_name": i.supplier_name, "customer_name": i.customer_name,
                       "subtotal": _f(i.subtotal), "tax": _f(i.tax), "total": _f(i.total), "currency": i.currency},
            "field_meta": i.field_meta or {}, "source": i.source or {}, "warnings": i.extraction_warnings or [],
            "line_items": [{"id": li.id, "position": li.position, "description": li.description, "quantity": li.quantity,
                            "unit_price": _f(li.unit_price), "amount": _f(li.amount)} for li in i.line_items],
            "issues": [{"id": x.id, "rule": x.rule, "severity": x.severity, "field": x.field, "message": x.message,
                        "details": x.details} for x in i.issues],
            "corrections": [{"field": c.field, "old": c.old_value, "new": c.new_value, "by": c.corrected_by,
                             "at": _dt(c.corrected_at)} for c in i.corrections]}
