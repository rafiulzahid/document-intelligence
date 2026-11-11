"""Turn a raw extraction (from the LLM or the rules baseline) into a normalised, grounded invoice.

Grounding = every value must be findable in the source text. A value the model returns that is
not in the document is discarded (set to null, status "removed") instead of being trusted. This is
how the system "handles missing or uncertain information without inventing values".
"""
from __future__ import annotations

import re
from typing import Any, Optional

from ..normalize import (norm_id, norm_text, normalize_currency, numbers_in_text, parse_amount,
                         parse_date)
from .schema import AMOUNT_FIELDS, HEADER_FIELDS, ExtractedInvoice, FieldMeta, LineItem

LOW_CONFIDENCE = 0.5
NULLS = {"", "null", "none", "n/a", "na", "-", "unknown", "not found", "not available"}

try:
    from rapidfuzz import fuzz as _fuzz
except ImportError:  # pragma: no cover
    _fuzz = None


def partial_match(needle: str, hay: str) -> float:
    """0-100 similarity of `needle` to the best-matching part of `hay` (both normalised)."""
    n, h = norm_text(needle), norm_text(hay)
    if not n:
        return 0.0
    if n in h:
        return 100.0
    if _fuzz is not None:
        return float(_fuzz.partial_ratio(n, h))
    toks = n.split()
    present = sum(t in h.split() for t in toks)
    return 100.0 * present / max(1, len(toks))


def _entry(raw: Any) -> tuple[Optional[str], Optional[str], Optional[float]]:
    """Accept {"value","evidence","confidence"} or a bare value."""
    if isinstance(raw, dict):
        v, ev, c = raw.get("value"), raw.get("evidence"), raw.get("confidence")
    else:
        v, ev, c = raw, None, None
    if v is not None and not isinstance(v, str):
        v = str(v)
    if v is not None and v.strip().lower() in NULLS:
        v = None
    try:
        c = None if c is None else max(0.0, min(1.0, float(c)))
    except (TypeError, ValueError):
        c = None
    return (v.strip() if v else None), (str(ev).strip() if ev else None), c


def _currency_hint(text: str) -> Optional[bool]:
    """Day-first? Decided from unambiguous dates in the same text, then from currency/locale clues."""
    votes = {True: 0, False: 0}
    for a, b in re.findall(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](?:\d{2}|\d{4})\b", text):
        a, b = int(a), int(b)
        if a > 12 >= b:
            votes[True] += 1
        elif b > 12 >= a:
            votes[False] += 1
    if votes[True] != votes[False]:
        return votes[True] > votes[False]
    if re.search(r"\b(RM|EUR|GBP|MYR|SGD|INR|BDT|Tk|GST|SDN|BHD)\b|[€£₹৳]", text):
        return True
    if "$" in text:
        return False
    return None


def ground(raw: dict, text: str, method: str) -> ExtractedInvoice:
    inv = ExtractedInvoice(method=method)
    text_numbers = numbers_in_text(text)
    text_compact = re.sub(r"\s+", "", text).upper()
    text_ws = re.sub(r"\s+", " ", text).lower()

    for f in HEADER_FIELDS:
        v, ev, conf = _entry(raw.get(f))
        meta = FieldMeta(raw=v, evidence=ev, confidence=conf)
        value, grounded, note = None, False, None
        if v is not None:
            if f == "invoice_number":
                value = re.sub(r"^(no\.?|number|#|nr\.?)\s*[:#]?\s*", "", v, flags=re.I).strip() or v
                grounded = re.sub(r"\s+", "", value).upper() in text_compact
            elif f == "invoice_date":
                value, note = parse_date(v, prefer_dayfirst=_currency_hint(text))
                grounded = re.sub(r"\s+", " ", v).lower() in text_ws or re.sub(r"\s+", "", v).upper() in text_compact
                if value is None:
                    grounded = False
            elif f in AMOUNT_FIELDS:
                value = parse_amount(v)
                grounded = value is not None and abs(value) in text_numbers
            elif f == "currency":
                value, note = normalize_currency(v)
                grounded = value is not None and (v in text or v.upper() in text.upper())
            else:  # supplier / customer names
                value = re.sub(r"\s+", " ", v).strip(" ,;:")
                grounded = partial_match(value, text) >= 85
        if v is None:
            meta.status = "missing"
        elif not grounded:
            meta.status, meta.note = "removed", (note or "value not found in the source text; discarded")
            inv.warnings.append(f"{f}: model value '{v}' not found in the document, discarded")
            value = None
        else:
            meta.status = "found"
            if note and f == "invoice_date":          # ambiguous DD/MM vs MM/DD -> needs a human
                meta.status, meta.note = "uncertain", note
            elif note:                                # e.g. "$ read as USD": informational only
                meta.note = note
            if conf is not None and conf < LOW_CONFIDENCE:
                meta.status = "uncertain"
                meta.note = (meta.note + "; " if meta.note else "") + f"low confidence {conf:.2f}"
        setattr(inv, f, value)
        inv.field_meta[f] = meta

    removed_items = 0
    for it in raw.get("line_items") or []:
        if not isinstance(it, dict):
            continue
        desc = it.get("description")
        desc = str(desc).strip() if desc is not None and str(desc).strip().lower() not in NULLS else None
        li = LineItem(description=desc, quantity=parse_amount(it.get("quantity")),
                      unit_price=parse_amount(it.get("unit_price")), amount=parse_amount(it.get("amount")))
        for k in ("quantity", "unit_price", "amount"):
            val = getattr(li, k)
            if val is not None and abs(val) not in text_numbers:
                setattr(li, k, None); removed_items += 1
        if li.description is None and li.amount is None:
            continue
        inv.line_items.append(li)
    if removed_items:
        inv.warnings.append(f"line_items: {removed_items} number(s) not found in the document, discarded")
    return inv
