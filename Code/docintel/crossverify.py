"""Cross-verification between two sets of invoices (PDF <-> PDF, PDF <-> Excel ledger).

    report = cross_verify(left_invoices, right_invoices, left_name="PDFs", right_name="ledger")

Invoices are matched by invoice number. For each match the header fields and line items are
compared; invoices present on only one side are reported too.
"""
from __future__ import annotations

from typing import Iterable

from .extract.grounding import partial_match
from .extract.schema import ExtractedInvoice
from .normalize import norm_id, norm_text
from .validate import close

COMPARE = ["invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency"]


def _same(field, a, b) -> bool:
    if a is None or b is None:
        return True  # nothing to compare
    if field in ("subtotal", "tax", "total"):
        return close(float(a), float(b))
    if field in ("supplier_name", "customer_name"):
        return norm_text(a) == norm_text(b)
    return str(a).strip().upper() == str(b).strip().upper()


def cross_verify(left: Iterable[ExtractedInvoice], right: Iterable[ExtractedInvoice],
                 left_name: str = "left", right_name: str = "right") -> dict:
    L = {norm_id(i.invoice_number): i for i in left if i.invoice_number}
    R = {norm_id(i.invoice_number): i for i in right if i.invoice_number}
    mismatches = []
    for num in sorted(L.keys() & R.keys()):
        a, b = L[num], R[num]
        for f in COMPARE:
            va, vb = getattr(a, f), getattr(b, f)
            if not _same(f, va, vb):
                mismatches.append({"invoice_number": a.invoice_number, "field": f, left_name: va, right_name: vb})
        if a.line_items and b.line_items:
            if len(a.line_items) != len(b.line_items):
                mismatches.append({"invoice_number": a.invoice_number, "field": "line_items.count",
                                   left_name: len(a.line_items), right_name: len(b.line_items)})
            free = list(range(len(b.line_items)))
            for k, x in enumerate(a.line_items):
                j = max(free, key=lambda j: partial_match(x.description or "", b.line_items[j].description or ""), default=None)
                if j is None or partial_match(x.description or "", b.line_items[j].description or "") < 80:
                    j = k if k in free else None          # fall back to the same row position
                if j is None:
                    continue
                free.remove(j)
                y = b.line_items[j]
                for f in ("quantity", "unit_price", "amount"):
                    if getattr(x, f) is not None and getattr(y, f) is not None and not close(getattr(x, f), getattr(y, f)):
                        mismatches.append({"invoice_number": a.invoice_number, "field": f"line_items[{k}].{f}",
                                           "description": x.description, left_name: getattr(x, f), right_name: getattr(y, f)})
    only_left = [L[n].invoice_number for n in sorted(L.keys() - R.keys())]
    only_right = [R[n].invoice_number for n in sorted(R.keys() - L.keys())]
    return {"matched": len(L.keys() & R.keys()), "mismatches": mismatches,
            f"only_in_{left_name}": only_left, f"only_in_{right_name}": only_right,
            "consistent": not mismatches and not only_left and not only_right}
