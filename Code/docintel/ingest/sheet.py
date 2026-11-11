"""Spreadsheets: XLSX/XLSM (openpyxl), legacy XLS (xlrd), CSV/TSV (encoding + delimiter detection)."""
from __future__ import annotations

import csv
import io
import zipfile
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from .types import CORRUPT_FILE, IngestError, SheetTable


def _cell(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat() if v.time() == time(0, 0) else v.isoformat(sep=" ")
    if isinstance(v, date):
        return v.isoformat()
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return int(v)
    if isinstance(v, str):
        v = v.strip()
        return v or None
    return v


def _clean(rows: list[list[Any]]) -> list[list[Any]]:
    rows = [[_cell(v) for v in r] for r in rows]
    rows = [r for r in rows if any(v is not None for v in r)]
    if not rows:
        return []
    width = max(len(r) for r in rows)
    rows = [r + [None] * (width - len(r)) for r in rows]
    keep = [j for j in range(width) if any(r[j] is not None for r in rows)]
    return [[r[j] for j in keep] for r in rows]


def _table(name: str, rows: list[list[Any]]) -> SheetTable:
    rows = _clean(rows)
    text = "\n".join("\t".join("" if v is None else str(v) for v in r) for r in rows)
    warnings = []
    if len(rows) == 0:
        warnings.append("EMPTY_SHEET")
    elif len(rows) == 1:
        warnings.append("NO_DATA_ROWS (header only)")
    return SheetTable(name=name, rows=rows, text=text, n_rows=len(rows),
                      n_cols=max((len(r) for r in rows), default=0), warnings=warnings)


def read_xlsx(path: Path) -> list[SheetTable]:
    from openpyxl import load_workbook
    try:
        wb = load_workbook(path, read_only=True, data_only=True)  # data_only: cached formula results
    except (zipfile.BadZipFile, KeyError, OSError, ValueError) as e:
        raise IngestError(CORRUPT_FILE, f"Cannot open Excel workbook: {e}") from e
    except Exception as e:  # noqa: BLE001  (openpyxl InvalidFileException etc.)
        raise IngestError(CORRUPT_FILE, f"Cannot open Excel workbook: {e}") from e
    try:
        return [_table(ws.title, [list(r) for r in ws.iter_rows(values_only=True)]) for ws in wb.worksheets]
    finally:
        wb.close()


def read_xls(path: Path) -> list[SheetTable]:
    import xlrd
    try:
        book = xlrd.open_workbook(str(path))
    except Exception as e:  # noqa: BLE001
        raise IngestError(CORRUPT_FILE, f"Cannot open .xls workbook: {e}") from e
    out = []
    for sh in book.sheets():
        rows = []
        for r in range(sh.nrows):
            row = []
            for c in range(sh.ncols):
                cell = sh.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    row.append(xlrd.xldate_as_datetime(cell.value, book.datemode))
                elif cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                    row.append(None)
                else:
                    row.append(cell.value)
            rows.append(row)
        out.append(_table(sh.name, rows))
    return out


def decode_text(raw: bytes) -> tuple[str, str]:
    for enc in ("utf-8-sig",):
        try:
            return raw.decode(enc), "utf-8"
        except UnicodeDecodeError:
            pass
    try:
        from charset_normalizer import from_bytes
        best = from_bytes(raw).best()
        if best is not None and best.encoding:
            return str(best), best.encoding
    except ImportError:
        pass
    return raw.decode("cp1252", errors="replace"), "cp1252"


def read_csv(path: Path) -> tuple[list[SheetTable], list[str]]:
    raw = path.read_bytes()
    text, enc = decode_text(raw)
    sample = text[:20000]
    try:
        delim = csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        delim = max(",;\t|", key=lambda d: sample.count(d))
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    notes = [f"encoding={enc}", f"delimiter={'TAB' if delim == chr(9) else delim}"]
    return [_table(path.name, rows)], notes
