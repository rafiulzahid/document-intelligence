"""PDFs: use the text layer when a page has one, OCR the page image when it does not.

Handles mixed PDFs (some pages digital, some scanned), very long PDFs (pages are rendered and
OCR'd in small batches, in parallel when the OCR backend allows it), encrypted PDFs and
broken files. A page that fails is kept with a warning instead of failing the whole file.
"""
from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable, Optional

import pymupdf
from PIL import Image

from .image import ocr_image
from .layout import words_to_text
from .ocr import OcrBackend
from .types import CORRUPT_FILE, ENCRYPTED_PDF, IngestError, PageText

MIN_TEXT_CHARS = 40     # fewer alphanumeric characters than this -> treat the page as scanned
OCR_DPI = 300

try:  # keep MuPDF's own messages out of the console; we report problems ourselves
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
except Exception:  # noqa: BLE001  (older PyMuPDF)
    pass


def open_pdf(path: Path) -> pymupdf.Document:
    try:
        doc = pymupdf.open(path, filetype="pdf")
    except Exception as e:  # noqa: BLE001  (FileDataError, EmptyFileError, RuntimeError...)
        raise IngestError(CORRUPT_FILE, f"Cannot open PDF: {e}") from e
    if doc.needs_pass and not doc.authenticate(""):
        raise IngestError(ENCRYPTED_PDF, "The PDF is password-protected; provide an unlocked copy.")
    if doc.page_count == 0:
        raise IngestError(CORRUPT_FILE, "The PDF has no pages.")
    return doc


def _alnum(s: str) -> int:
    return sum(ch.isalnum() for ch in s)


def ingest_pdf(path: Path, backend_factory: Callable[[], OcrBackend],
               progress: Optional[Callable[[int, int], None]] = None,
               ocr_dpi: int = OCR_DPI, ocr_workers: Optional[int] = None) -> tuple[list[PageText], list[str]]:
    """Returns (pages, file-level warnings). backend_factory is only called if a page needs OCR."""
    doc = open_pdf(path)
    n = doc.page_count
    pages: list[Optional[PageText]] = [None] * n
    warnings: list[str] = []
    need_ocr: list[int] = []
    done = 0

    # pass 1: text layer (fast)
    for i in range(n):
        try:
            page = doc[i]
            words = [(w[0], w[1], w[2], w[3], w[4], None) for w in page.get_text("words")]
            text = words_to_text(words)
            if _alnum(text) >= MIN_TEXT_CHARS:
                pages[i] = PageText(page_no=i + 1, text=text, method="pdf_text",
                                    width=int(page.rect.width), height=int(page.rect.height))
                done += 1
                if progress:
                    progress(done, n)
            else:
                need_ocr.append(i)
        except Exception as e:  # noqa: BLE001
            pages[i] = PageText(page_no=i + 1, text="", method="failed", warnings=[f"PAGE_ERROR: {e}"])
            done += 1

    # pass 2: OCR the pages without a usable text layer, in batches
    if need_ocr:
        try:
            backend = backend_factory()
        except IngestError as e:
            for i in need_ocr:
                pages[i] = PageText(page_no=i + 1, text="", method="failed", warnings=[f"{e.code}: {e.message}"])
            warnings.append(f"{len(need_ocr)} scanned page(s) not read: {e.message}")
            need_ocr = []
            backend = None
        if backend is not None:
            workers = ocr_workers or (max(1, min(8, (os.cpu_count() or 2) // 2)) if backend.parallel_safe else 1)
            batch = max(1, workers * 2)
            with ThreadPoolExecutor(max_workers=workers) as pool:
                for start in range(0, len(need_ocr), batch):
                    idx = need_ocr[start:start + batch]
                    imgs = {}
                    for i in idx:  # rendering stays in this thread (MuPDF objects are not thread-safe)
                        try:
                            pix = doc[i].get_pixmap(dpi=ocr_dpi, colorspace=pymupdf.csGRAY)
                            imgs[i] = Image.frombytes("L", (pix.width, pix.height), pix.samples)
                        except Exception as e:  # noqa: BLE001
                            pages[i] = PageText(page_no=i + 1, text="", method="failed", warnings=[f"RENDER_ERROR: {e}"])
                    futs = {i: pool.submit(ocr_image, im, backend, i + 1) for i, im in imgs.items()}
                    for i, f in futs.items():
                        try:
                            pages[i] = f.result()
                        except Exception as e:  # noqa: BLE001
                            pages[i] = PageText(page_no=i + 1, text="", method="failed", warnings=[f"OCR_ERROR: {e}"])
                        done += 1
                        if progress:
                            progress(done, n)
    repaired = bool(getattr(doc, "is_repaired", False))
    doc.close()

    out = [p for p in pages if p is not None]
    failed = sum(p.method == "failed" for p in out)
    if failed == len(out):
        raise IngestError(CORRUPT_FILE, "No page of the PDF could be read.")
    if repaired:
        if sum(_alnum(p.text) for p in out) < MIN_TEXT_CHARS:
            raise IngestError(CORRUPT_FILE, "The PDF is damaged (truncated or broken) and has no readable content.")
        warnings.append("PDF_REPAIRED: the file was damaged; MuPDF repaired it, content may be incomplete")
    if failed:
        warnings.append(f"{failed} of {len(out)} pages could not be read")
    return out, warnings
