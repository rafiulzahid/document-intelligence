"""Validation rules for extracted invoices.

    issues = validate_invoice(inv)                         # rules on one invoice
    dup = find_duplicates(records)                         # across a batch / the database
    status = review_status(issues)                         # "valid" | "needs_review"

Each issue: {"rule", "severity" (error|warning|info), "field", "message"}.
Any error or warning -> the invoice needs manual review.
"""
from __future__ import annotations

from datetime import date
from typing import Iterable, Optional

from .extract.schema import ExtractedInvoice
from .normalize import norm_id, norm_text

REQUIRED = ["invoice_number", "invoice_date", "supplier_name", "total"]
ABS_TOL = 0.02      # covers cent rounding (e.g. receipts rounded to 0.05)
REL_TOL = 0.0001    # 0.01 %: large amounts with many lines; a real $7.70 difference on $1,750 is still caught
MAX_TAX_RATE = 0.30


def close(a: float, b: float) -> bool:
    return abs(a - b) <= max(ABS_TOL, REL_TOL * max(abs(a), abs(b)))


def _issue(rule, severity, message, field=None, **extra):
    d = {"rule": rule, "severity": severity, "field": field, "message": message}
    d.update(extra)
    return d


def validate_invoice(inv: ExtractedInvoice, today: Optional[date] = None) -> list[dict]:
    today = today or date.today()
    issues = []
    # 1. required fields
    for f in REQUIRED:
        if getattr(inv, f) in (None, ""):
            st = inv.field_meta.get(f).status if f in inv.field_meta else "missing"
            why = "value not found in the document" if st == "removed" else "not present in the document"
            issues.append(_issue("MISSING_FIELD", "warning", f"{f} is missing ({why})", f))
    # 2. numbers
    for f in ("subtotal", "tax", "total"):
        v = getattr(inv, f)
        if v is not None and v < 0:
            issues.append(_issue("NEGATIVE_AMOUNT", "warning", f"{f} is negative ({v})", f))
    for i, li in enumerate(inv.line_items):
        for k in ("quantity", "unit_price", "amount"):
            v = getattr(li, k)
            if v is not None and v < 0:
                issues.append(_issue("NEGATIVE_AMOUNT", "info", f"line {i + 1} {k} is negative ({v})", f"line_items[{i}].{k}"))
    # 3. dates
    if inv.invoice_date:
        try:
            d = date.fromisoformat(inv.invoice_date)
            if d > today:
                issues.append(_issue("FUTURE_DATE", "warning", f"invoice date {d} is in the future", "invoice_date"))
            if d.year < 1990:
                issues.append(_issue("IMPLAUSIBLE_DATE", "warning", f"invoice date {d} is implausibly old", "invoice_date"))
        except ValueError:
            issues.append(_issue("INVALID_DATE", "error", f"invalid date '{inv.invoice_date}'", "invoice_date"))
    # 4. line item arithmetic
    for i, li in enumerate(inv.line_items):
        if None not in (li.quantity, li.unit_price, li.amount):
            expected = round(li.quantity * li.unit_price, 2)
            if not close(expected, li.amount):
                issues.append(_issue("LINE_ITEM_MATH", "error",
                                     f"line {i + 1}: {li.quantity:g} x {li.unit_price:.2f} = {expected:.2f}, "
                                     f"but the line amount is {li.amount:.2f}", f"line_items[{i}].amount",
                                     expected=expected, actual=li.amount))
    # 5. line items vs subtotal / total
    amounts = [li.amount for li in inv.line_items]
    if amounts and None not in amounts:
        s = round(sum(amounts), 2)
        if inv.subtotal is not None:
            if not close(s, inv.subtotal):
                issues.append(_issue("LINE_SUM_MISMATCH", "error",
                                     f"line items add up to {s:.2f}, but the subtotal is {inv.subtotal:.2f}",
                                     "subtotal", expected=s, actual=inv.subtotal))
        elif inv.total is not None and not inv.tax:
            if not close(s, inv.total):
                issues.append(_issue("LINE_SUM_MISMATCH", "error",
                                     f"line items add up to {s:.2f}, but the total is {inv.total:.2f} (no tax shown)",
                                     "total", expected=s, actual=inv.total))
    elif amounts:
        issues.append(_issue("LINE_ITEMS_INCOMPLETE", "info", "some line amounts are missing; sums not checked", "line_items"))
    # 6. subtotal + tax = total
    if None not in (inv.subtotal, inv.tax, inv.total):
        exp = round(inv.subtotal + inv.tax, 2)
        if not close(exp, inv.total):
            issues.append(_issue("TOTAL_MISMATCH", "error",
                                 f"subtotal {inv.subtotal:.2f} + tax {inv.tax:.2f} = {exp:.2f}, but the total is {inv.total:.2f}",
                                 "total", expected=exp, actual=inv.total))
        if inv.subtotal > 0 and not (0 <= inv.tax / inv.subtotal <= MAX_TAX_RATE):
            issues.append(_issue("TAX_RATE_IMPLAUSIBLE", "warning",
                                 f"tax is {100 * inv.tax / inv.subtotal:.1f}% of the subtotal", "tax"))
    elif inv.subtotal is not None and inv.total is not None and inv.tax is None and inv.total + ABS_TOL < inv.subtotal:
        issues.append(_issue("TOTAL_MISMATCH", "error", f"total {inv.total:.2f} is less than subtotal {inv.subtotal:.2f}", "total"))
    # 7. extraction quality
    for f, m in inv.field_meta.items():
        if m.status == "uncertain":
            issues.append(_issue("UNCERTAIN_VALUE", "warning", f"{f}: {m.note or 'low confidence'}", f))
        elif m.status == "removed":
            issues.append(_issue("UNGROUNDED_VALUE", "warning",
                                 f"{f}: the extracted value '{m.raw}' does not appear in the document and was discarded", f))
    return issues


def duplicate_keys(inv: ExtractedInvoice) -> list[str]:
    keys = []
    party = norm_text(inv.supplier_name) or norm_text(inv.customer_name)
    if inv.invoice_number:
        keys.append(f"num:{norm_id(inv.invoice_number)}|{party}")
    if inv.invoice_number and inv.total is not None:
        keys.append(f"numtot:{norm_id(inv.invoice_number)}|{inv.total:.2f}")
    if inv.total is not None and inv.invoice_date and party:
        keys.append(f"sum:{party}|{inv.invoice_date}|{inv.total:.2f}")
    return keys


def find_duplicates(records: Iterable[tuple[str, ExtractedInvoice, Optional[str]]]) -> dict[str, list[dict]]:
    """records: (record_id, invoice, file_sha256) in processing order.
    Returns {record_id: [DUPLICATE issues]} for every record that repeats an earlier one."""
    seen: dict[str, str] = {}
    out: dict[str, list[dict]] = {}
    doc_of = lambda r: r.rsplit("#", 1)[0]  # noqa: E731  record ids look like "<document>#<invoice index>"
    has_number: dict[str, bool] = {}
    for rid, inv, sha in records:
        hits = []
        has_number[rid] = bool(inv.invoice_number)
        keys = ([f"sha:{sha}"] if sha else []) + duplicate_keys(inv)
        for k in keys:
            if k in seen and seen[k] != rid:
                if k.startswith("sha:") and doc_of(seen[k]) == doc_of(rid):
                    continue  # several invoices inside one file share the file hash
                if k.startswith("sum:") and has_number[rid] and has_number[seen[k]]:
                    continue  # different invoice numbers: same party/date/total can be legitimate
                hits.append((k, seen[k]))
        for k in keys:
            seen.setdefault(k, rid)
        if hits:
            kind, first = hits[0]
            how = {"sha": "identical file", "num": "same invoice number and party", "numtot": "same invoice number and total",
                   "sum": "same party, date and total"}[kind.split(":")[0]]
            out[rid] = [_issue("DUPLICATE", "error", f"duplicate of {first} ({how})", "invoice_number",
                               duplicate_of=first)]
    return out


def review_status(issues: list[dict]) -> str:
    return "needs_review" if any(i["severity"] in ("error", "warning") for i in issues) else "valid"
