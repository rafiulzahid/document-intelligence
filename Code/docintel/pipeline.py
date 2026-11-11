"""End-to-end processing of one file: ingest -> extract -> validate.

    from docintel.pipeline import process_file
    record = process_file("invoice.pdf")          # dict, JSON-serialisable

The API's background worker calls exactly this function; duplicate detection across documents
is done by the caller (the database or the batch runner), because it needs the other records.
"""
from __future__ import annotations

import time
from typing import Callable, Optional

from .extract import extract_document
from .extract.llm import LLMClient
from .ingest import ingest_file
from .ingest.types import IngestResult
from .validate import review_status, validate_invoice


def process_ingested(res: IngestResult, llm: Optional[LLMClient] = None) -> dict:
    t0 = time.perf_counter()
    invoices, info = extract_document(res, llm)
    out_invoices = []
    for k, inv in enumerate(invoices):
        issues = validate_invoice(inv)
        out_invoices.append({"index": k, "invoice": inv.to_dict(), "issues": issues,
                             "status": review_status(issues)})
    status = "failed" if not res.ok else ("no_invoice" if not invoices else
                                          ("needs_review" if any(i["status"] == "needs_review" for i in out_invoices) else "valid"))
    return {"file_name": res.file_name, "sha256": res.sha256, "detected_type": res.detected_type,
            "status": status, "error_code": res.error_code, "error_message": res.error_message,
            "pages": res.page_count, "ocr_pages": res.ocr_pages, "sheets": len(res.sheets),
            "ingest_warnings": res.warnings, "invoices": out_invoices, "extraction": info,
            "timings_s": {"ingest": round(res.timings_ms.get("total", 0) / 1000, 3),
                          "extract_validate": round(time.perf_counter() - t0, 3)}}


def process_file(path, llm: Optional[LLMClient] = None, ocr: str = "auto",
                 progress: Optional[Callable[[int, int], None]] = None) -> dict:
    res = ingest_file(path, ocr=ocr, progress=progress)
    rec = process_ingested(res, llm if llm is not None else LLMClient())
    rec["timings_s"]["total"] = round(rec["timings_s"]["ingest"] + rec["timings_s"]["extract_validate"], 3)
    return rec
