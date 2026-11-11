"""Document ingestion: any supported file -> text pages and/or sheet tables.

    from docintel.ingest import ingest_file
    r = ingest_file("invoice.pdf")
    if r.ok:
        print(r.full_text())
    else:
        print(r.error_code, r.error_message)

ingest_file() never raises: every problem becomes status="failed" with a stable error_code.
"""
from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Callable, Optional

from .detect import detect_type
from .ocr import get_backend
from .types import (FILE_TOO_LARGE, INTERNAL_ERROR, IngestError, IngestResult, PageText,  # noqa: F401
                    SheetTable)

MAX_FILE_MB = 200


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ingest_file(path, ocr: str = "auto", progress: Optional[Callable[[int, int], None]] = None,
                max_file_mb: float = MAX_FILE_MB, ocr_dpi: int = 300) -> IngestResult:
    path = Path(path)
    t0 = time.perf_counter()
    size = path.stat().st_size if path.exists() else 0
    res = IngestResult(path=str(path), file_name=path.name, size_bytes=size,
                       sha256=_sha256(path) if path.exists() else "", detected_type=None)
    try:
        if not path.exists():
            raise IngestError("NOT_FOUND", f"File not found: {path}")
        if size > max_file_mb * 1024 * 1024:
            raise IngestError(FILE_TOO_LARGE, f"File is {size / 1e6:.1f} MB; the limit is {max_file_mb} MB.")
        kind = detect_type(path)
        res.detected_type = kind
        ext = path.suffix.lower().lstrip(".")
        expected = {"pdf": {"pdf"}, "image": {"jpg", "jpeg", "png", "bmp", "gif", "webp"}, "tiff": {"tif", "tiff"},
                    "xlsx": {"xlsx", "xlsm"}, "xls": {"xls"}, "csv": {"csv", "tsv", "txt"}}[kind]
        if ext and ext not in expected:
            res.warnings.append(f"EXTENSION_MISMATCH: named .{ext} but content is {kind}")
        t1 = time.perf_counter()
        res.timings_ms["detect"] = round((t1 - t0) * 1000, 1)

        if kind == "pdf":
            from .pdf import ingest_pdf
            res.pages, w = ingest_pdf(path, lambda: get_backend(ocr), progress=progress, ocr_dpi=ocr_dpi)
            res.warnings += w
        elif kind in ("image", "tiff"):
            from .image import ingest_image
            res.pages = ingest_image(path, get_backend(ocr), progress=progress)
        elif kind == "xlsx":
            from .sheet import read_xlsx
            res.sheets = read_xlsx(path)
        elif kind == "xls":
            from .sheet import read_xls
            res.sheets = read_xls(path)
        elif kind == "csv":
            from .sheet import read_csv
            res.sheets, notes = read_csv(path)
            res.warnings += notes

        if res.sheets and all(s.n_rows <= 1 for s in res.sheets):
            res.warnings.append("NO_DATA_ROWS: the spreadsheet has no data rows")
        if res.pages and not any(p.text.strip() for p in res.pages):
            res.warnings.append("NO_TEXT_FOUND: no readable text on any page")
        res.timings_ms["extract"] = round((time.perf_counter() - t1) * 1000, 1)
    except IngestError as e:
        res.status, res.error_code, res.error_message = "failed", e.code, e.message
    except Exception as e:  # noqa: BLE001  (never crash the caller)
        res.status, res.error_code, res.error_message = "failed", INTERNAL_ERROR, f"{type(e).__name__}: {e}"
    res.timings_ms["total"] = round((time.perf_counter() - t0) * 1000, 1)
    return res
