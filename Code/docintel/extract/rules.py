"""Regex baseline extractor (no LLM).

Used when no LLM is configured or the LLM call fails, and as the baseline the evaluation report
compares the LLM against. Its output has the same shape as the LLM's, so it goes through the
same grounding and normalisation.
"""
from __future__ import annotations

import re

NUM = (r"[-+]?\(?(?:RM|[$€£])?\s?\d{1,3}(?:[ ,.']\d{3})+(?:[.,]\d{1,2})?\)?|"
       r"[-+]?\(?(?:RM|[$€£])?\s?\d+(?:[.,]\d{1,2})?\)?")
DATE = (r"\d{4}[./-]\d{1,2}[./-]\d{1,2}|\d{1,2}[./-]\d{1,2}[./-]\d{2,4}|"
        r"\d{1,2}[ -](?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[ -]\d{2,4}")


TITLE = re.compile(r"(?i)(tax\s+)?(invoice|receipt|bill|cash\s*sale|official receipt|statement|sales\s+invoice|"
                   r"credit\s+note|quotation|order)\s*(copy)?")


def _nums(line: str):
    """Numbers on a line with positions; skips percentages and inch marks (12" table top)."""
    from ..normalize import parse_amount
    out = []
    for m in re.finditer(r"\d+(?:[.,']\d{3})*(?:[.,]\d{1,2})?|\d+", line):
        nxt = line[m.end():m.end() + 1]
        if nxt in ('%', '"') or nxt.isalpha():
            continue
        v = parse_amount(m.group(0))
        if v is not None:
            out.append((m.start(), m.group(0), v))
    return out


def _is_int_token(tok: str) -> bool:
    return re.fullmatch(r"\d+", tok) is not None or re.fullmatch(r"\d+[.,]0{1,2}", tok) is not None


def _line_items(lines):
    """Item rows: description, then numbers where quantity x unit price = amount (amount to the right).
    Rows with the same column layout whose arithmetic does NOT add up are kept too, so validation can flag them."""
    skip = re.compile(r"(?i)\btotal|summary|sub-?total|vat \[%\]|balance|change|cash|rounding|\btax\b|:")
    rows = []
    for l in lines:
        if skip.search(l) or re.search(DATE, l, re.I):
            continue
        body = re.sub(r"^\s*\d+[.)]\s+", "", l)          # drop a leading row number "1."
        if not re.search(r"[A-Za-z]{3,}", body[:40]):
            continue
        nums = _nums(body)
        if len(nums) >= 3:
            rows.append((body, nums))

    def solve(nums):
        n = len(nums)
        for ia in range(n - 1, 1, -1):
            a = nums[ia][2]
            pairs = [(i, j) for i in range(ia - 1, -1, -1) for j in range(i - 1, -1, -1)]
            for i, j in pairs:
                x, y = nums[j], nums[i]
                if x[2] and y[2] and abs(x[2] * y[2] - a) <= max(0.011, 0.005 * a):
                    # quantity: the token without a decimal separator, else the integer-valued one, else the left one
                    def rank(t):
                        return (0 if re.fullmatch(r"\d+", t) else 1, 0 if _is_int_token(t) else 1)
                    if rank(y[1]) < rank(x[1]):
                        return (i - n, j - n, ia - n)
                    return (j - n, i - n, ia - n)
        return None

    solved = [(b, nums, solve(nums)) for b, nums in rows]
    layouts = {}
    for _, nums, f in solved:
        if f:
            layouts[(len(nums), f)] = layouts.get((len(nums), f), 0) + 1
    common = max(layouts, key=layouts.get) if layouts else None
    items = []
    for body, nums, f in solved:
        if f is None and common and len(nums) == common[0]:
            f = common[1]
        if f is None:
            continue
        iq, ip, ia = f
        start = min(nums[iq][0], nums[ip][0])
        desc = body[:start].strip(" $€£-\t") or None
        items.append({"description": desc, "quantity": nums[iq][1], "unit_price": nums[ip][1], "amount": nums[ia][1]})
    return items


def _f(value, evidence, conf=0.6):
    return {"value": value, "evidence": evidence, "confidence": conf} if value else {"value": None, "evidence": None, "confidence": None}


def _last_number(line: str):
    nums = [n.strip() for n in re.findall(NUM, line) if re.search(r"\d", n)]
    nums = [n for n in nums if not re.fullmatch(r"\d{1,2}\s*%?", n.strip()) or "." in n or "," in n]
    return nums[-1].strip() if nums else None


def rules_extract(text: str) -> dict:
    lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("[PAGE") and not l.startswith("[SHEET")]
    low = [l.lower() for l in lines]
    out = {}

    m = re.search(r"(?:invoice|inv|bill|receipt|order)\s*(?:no|number|num|nr|#|id)\.?\s*[:#.]?\s*([A-Z0-9][A-Z0-9\-/]{2,})", text, re.I)
    out["invoice_number"] = _f(m and m.group(1), m and m.group(0))

    m = re.search(rf"(?:invoice\s*date|date\s*of\s*issue|issue\s*date|date)\s*[:.]?\s*({DATE})", text, re.I) \
        or re.search(rf"({DATE})", text, re.I)
    out["invoice_date"] = _f(m and m.group(1), m and m.group(0))

    supplier = customer = None
    for i, l in enumerate(low):
        if "seller" in l and "client" in l and i + 1 < len(lines):
            parts = re.split(r"\s{3,}", lines[i + 1])
            supplier, customer = parts[0], parts[1] if len(parts) > 1 else None
            break
    for pat, key in [(r"(?:seller|supplier|vendor|from)\s*:\s*(.+)", "s"), (r"(?:customer name|customer|client|bill to|buyer)\s*:\s*(.+)", "c")]:
        m = re.search(pat, text, re.I)
        if m:
            val = re.split(r"\s{3,}", m.group(1).strip())[0]
            if key == "s" and not supplier and len(val) > 2:
                supplier = val
            if key == "c" and not customer and len(val) > 2:
                customer = val
    if not supplier and lines:  # receipts: the shop name is usually the first line with letters
        first = next((l for l in lines[:3] if re.search(r"[A-Za-z]{3,}", l) and not TITLE.fullmatch(l.strip())
                      and ":" not in l and "   " not in l), None)
        out["supplier_name"] = _f(first, first, 0.4)
    else:
        out["supplier_name"] = _f(supplier, supplier)
    out["customer_name"] = _f(customer, customer)

    def find(patterns, exclude=()):
        for l, ll in reversed(list(zip(lines, low))):
            if any(re.search(p, ll) for p in patterns) and not any(re.search(x, ll) for x in exclude):
                n = _last_number(l)
                if n:
                    return n, l
        return None, None

    v, ev = find([r"\bsub[- ]?total\b", r"\btotal net\b", r"\bnet (?:amount|total)\b"])
    out["subtotal"] = _f(v, ev)
    v, ev = find([r"\b(?:total )?(?:tax|vat|gst)\b(?! *(?:id|no|reg|%))"], exclude=[r"tax id", r"reg"])
    out["tax"] = _f(v, ev, 0.5)
    v, ev = find([r"\bgrand total\b", r"\btotal (?:due|amount|payable)\b", r"\bamount due\b"]) if \
        any(re.search(r"grand total|total due|amount due|total payable|total amount", l) for l in low) else \
        find([r"\btotal\b"], exclude=[r"sub[- ]?total", r"total qty", r"total tax", r"total vat", r"total net"])
    out["total"] = _f(v, ev)
    if ev and not out["subtotal"]["value"]:
        # summary rows like "Total  $ 889,20  $ 88,92  $ 978,12" = net, tax, gross
        from ..normalize import parse_amount
        three = [n.strip() for n in re.findall(NUM, ev) if re.search(r"\d", n)][-3:]
        vals = [parse_amount(x) for x in three]
        if len(three) == 3 and None not in vals and abs(vals[0] + vals[1] - vals[2]) <= 0.011:
            out["subtotal"], out["tax"] = _f(three[0], ev), _f(three[1], ev)

    m = re.search(r"(RM|US\$|S\$|€|£|\$|\bEUR\b|\bUSD\b|\bGBP\b|\bMYR\b|\bBDT\b)", text)
    out["currency"] = _f(m and m.group(1), m and m.group(0), 0.6)
    out["line_items"] = _line_items(lines)
    return out
