"""Data structures returned by ingestion. Plain dataclasses so they serialise to JSON easily."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional


class IngestError(Exception):
    """Raised inside ingestion code; converted to a failed IngestResult by ingest_file()."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# Error codes (stable strings: the API, the UI and the tests rely on them)
EMPTY_FILE = "EMPTY_FILE"
UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE"
CORRUPT_FILE = "CORRUPT_FILE"
ENCRYPTED_PDF = "ENCRYPTED_PDF"
FILE_TOO_LARGE = "FILE_TOO_LARGE"
OCR_UNAVAILABLE = "OCR_UNAVAILABLE"
NO_CONTENT = "NO_CONTENT"
INTERNAL_ERROR = "INTERNAL_ERROR"


@dataclass
class PageText:
    page_no: int                    # 1-based (page of a PDF, frame of a TIFF, or 1 for an image)
    text: str                       # layout-preserving text: one visual line per line
    method: str                     # "pdf_text" | "ocr:<backend>"
    ocr_confidence: Optional[float] = None   # mean word confidence 0-1 (OCR pages only)
    width: Optional[int] = None
    height: Optional[int] = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class SheetTable:
    name: str                       # sheet name, or the file name for CSV
    rows: list[list[Any]]           # cleaned cell values (empty rows/columns removed)
    text: str                       # tab-separated text of the rows, for the LLM and search
    n_rows: int = 0
    n_cols: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass
class IngestResult:
    path: str
    file_name: str
    size_bytes: int
    sha256: str
    detected_type: Optional[str]    # pdf | image | tiff | xlsx | xls | csv | None
    status: str = "ok"              # ok | failed
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    pages: list[PageText] = field(default_factory=list)
    sheets: list[SheetTable] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def ocr_pages(self) -> int:
        return sum(p.method.startswith("ocr") for p in self.pages)

    def full_text(self) -> str:
        """All content as one string with page / sheet markers (what the LLM receives)."""
        parts = [f"[PAGE {p.page_no}]\n{p.text}" for p in self.pages]
        parts += [f"[SHEET {s.name}]\n{s.text}" for s in self.sheets]
        return "\n\n".join(parts)

    @classmethod
    def from_dict(cls, d: dict) -> "IngestResult":
        keep = {k: v for k, v in d.items() if k in cls.__dataclass_fields__}
        keep["pages"] = [PageText(**p) for p in d.get("pages", [])]
        keep["sheets"] = [SheetTable(**s) for s in d.get("sheets", [])]
        return cls(**keep)

    def to_dict(self, include_text: bool = True) -> dict:
        d = asdict(self)
        d.update(page_count=self.page_count, ocr_pages=self.ocr_pages)
        if not include_text:
            for p in d["pages"]:
                p["text"] = f"<{len(p['text'])} chars>"
            for s in d["sheets"]:
                s["rows"] = f"<{len(s['rows'])} rows>"
                s["text"] = f"<{len(s['text'])} chars>"
        return d
