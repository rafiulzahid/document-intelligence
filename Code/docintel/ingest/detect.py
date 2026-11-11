"""Detect a file's real type from its bytes, not its extension."""
from __future__ import annotations

import zipfile
from pathlib import Path

from .types import CORRUPT_FILE, EMPTY_FILE, UNSUPPORTED_TYPE, IngestError

KNOWN_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp",
             ".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".txt"}


def detect_type(path: Path) -> str:
    """Return one of: pdf, image, tiff, xlsx, xls, csv. Raises IngestError otherwise."""
    size = path.stat().st_size
    if size == 0:
        raise IngestError(EMPTY_FILE, "The file is empty (0 bytes).")
    with open(path, "rb") as f:
        head = f.read(2048)

    if b"%PDF-" in head[:1024]:
        return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n") or head.startswith(b"\xff\xd8\xff") \
            or head[:6] in (b"GIF87a", b"GIF89a") or head.startswith(b"BM") \
            or (head.startswith(b"RIFF") and head[8:12] == b"WEBP"):
        return "image"
    if head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"):
        return "tiff"
    if head.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):  # OLE2 container (legacy .xls)
        return "xls"
    if head.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
        except zipfile.BadZipFile as e:
            raise IngestError(CORRUPT_FILE, f"Broken ZIP/Excel container: {e}") from e
        if any(n.startswith("xl/") for n in names):
            return "xlsx"
        raise IngestError(UNSUPPORTED_TYPE, "ZIP file that is not an Excel workbook (.docx/.zip are not supported).")
    if _looks_like_text(head):
        return "csv"
    ext = path.suffix.lower()
    if ext in KNOWN_EXT:
        raise IngestError(CORRUPT_FILE, f"File content is not a valid {ext} file (corrupted or wrong extension).")
    raise IngestError(UNSUPPORTED_TYPE, f"Unsupported file type (extension '{ext or 'none'}').")


def _looks_like_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    sample = head[:2048]
    for enc in ("utf-8", "cp1252"):
        try:
            txt = sample.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        return False
    printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in txt)
    return len(txt) > 0 and printable / len(txt) > 0.95
