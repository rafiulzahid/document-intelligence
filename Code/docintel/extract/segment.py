"""Split an ingested document into invoice-sized units.

* spreadsheets: sheets with a recognisable structure are mapped directly (no LLM); other sheets
  become one text unit each.
* PDFs / multi-frame TIFFs: a new invoice starts on a page that shows an invoice number different
  from the current one; pages without a number continue the current invoice. A 1000-page PDF with
  one invoice per page becomes 1000 units; a 3-page invoice stays one unit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ..ingest.types import IngestResult
from .schema import ExtractedInvoice
from .tables import map_sheet

INV_NO = re.compile(r"(?:invoice|inv|bill|receipt)\s*(?:no|number|num|nr|#|id)\.?\s*[:#.]?\s*([A-Z0-9][A-Z0-9\-/]{2,})", re.I)


@dataclass
class Unit:
    text: str
    source: dict = field(default_factory=dict)          # {"pages": [1, 2]} or {"sheet": "Sheet1"}
    mapped: Optional[list[ExtractedInvoice]] = None     # already extracted deterministically


def page_invoice_number(text: str) -> Optional[str]:
    m = INV_NO.search(text[:3000])
    return m.group(1).upper() if m else None


def split_units(res: IngestResult) -> list[Unit]:
    units: list[Unit] = []
    for s in res.sheets:
        mapped = map_sheet(s)
        if mapped:
            units.append(Unit(text=s.text, source={"sheet": s.name}, mapped=mapped))
        elif s.n_rows > 1:
            units.append(Unit(text=f"[SHEET {s.name}]\n{s.text}", source={"sheet": s.name}))
    pages = [p for p in res.pages if p.text.strip()]
    cur: Optional[Unit] = None
    cur_no: Optional[str] = None
    for p in pages:
        no = page_invoice_number(p.text)
        if cur is None or (no is not None and cur_no is not None and no != cur_no):
            cur = Unit(text="", source={"pages": []})
            units.append(cur)
            cur_no = no
        elif cur_no is None and no is not None:
            cur_no = no
        cur.text += ("\n\n" if cur.text else "") + f"[PAGE {p.page_no}]\n{p.text}"
        cur.source["pages"].append(p.page_no)
    return units
