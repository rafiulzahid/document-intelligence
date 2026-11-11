"""Deterministic extraction for spreadsheets (no LLM needed when the structure is clear).

Two layouts are recognised:
  1. flat table  - a header row with an invoice-number column; one row per line item or per invoice
                   (ledgers, exports, CSV files). Rows are grouped by invoice number.
  2. key-value   - "Invoice Number | 10253" style label cells, an item table, a "Total:" cell.
Anything else is sent to the LLM as text.
"""
from __future__ import annotations

from typing import Any, Optional

from ..ingest.types import SheetTable
from ..normalize import norm_text, normalize_currency, parse_amount, parse_date
from .schema import ExtractedInvoice, FieldMeta, LineItem

SYN = {
    "invoice_number": {"invoice number", "invoice no", "invoice", "invoice id", "inv no", "invoice num",
                       "invoice nr", "bill no", "document number", "invoice #"},
    "invoice_date": {"invoice date", "date", "date of issue", "issue date", "bill date"},
    "supplier_name": {"supplier", "supplier name", "vendor", "vendor name", "seller", "seller name"},
    "customer_name": {"customer", "customer name", "client", "client name", "buyer", "bill to", "company name"},
    "subtotal": {"subtotal", "sub total", "net total", "total net", "net amount", "total net worth"},
    "tax": {"tax", "vat", "gst", "tax amount", "total vat", "sales tax"},
    "total": {"invoice total", "total", "grand total", "total amount", "amount due", "total due", "total gross"},
    "currency": {"currency", "currency code"},
    "description": {"product", "product name", "description", "item", "item description", "products", "service"},
    "quantity": {"quantity", "qty", "units"},
    "unit_price": {"unit price", "price", "unit cost", "rate", "net price"},
    "amount": {"line total", "amount", "total price", "line amount", "net worth", "extended price"},
}
ITEM_COLS = {"description", "quantity", "unit_price", "amount"}


def _key(cell) -> Optional[str]:
    if cell is None:
        return None
    t = norm_text(str(cell).replace("_", " ")).rstrip(":").strip()
    for field, names in SYN.items():
        if t in names:
            return field
    return None


def _meta(sheet: str, where: str) -> FieldMeta:
    return FieldMeta(status="found", confidence=1.0, evidence=f"{sheet}: {where}")


def _set(inv: ExtractedInvoice, field: str, value: Any, sheet: str, where: str):
    if value is None or getattr(inv, field) is not None:
        return
    if field in ("subtotal", "tax", "total"):
        value = parse_amount(value)
    elif field == "invoice_date":
        value, note = parse_date(value)
    elif field == "currency":
        value, _ = normalize_currency(value)
    else:
        value = str(value).strip()
    if value is None:
        return
    setattr(inv, field, value)
    inv.field_meta[field] = _meta(sheet, where)


def _finish(inv: ExtractedInvoice, sheet: str) -> ExtractedInvoice:
    for f in ["invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency"]:
        inv.field_meta.setdefault(f, FieldMeta(status="missing"))
    inv.method = "table_mapping"
    inv.source.setdefault("sheet", sheet)
    return inv


def map_flat_table(sheet: SheetTable) -> Optional[list[ExtractedInvoice]]:
    rows = sheet.rows
    for h, row in enumerate(rows[:10]):
        cols = {}
        for j, cell in enumerate(row):
            k = _key(cell)
            if k and k not in cols:
                cols[k] = j
        if "invoice_number" in cols and len(cols) >= 3:
            break
    else:
        return None
    groups: dict[str, list[tuple[int, list]]] = {}
    for i, row in enumerate(rows[h + 1:], start=h + 2):
        num = row[cols["invoice_number"]] if cols["invoice_number"] < len(row) else None
        if num is None:
            continue
        groups.setdefault(str(num).strip(), []).append((i, row))
    out = []
    for num, items in groups.items():
        inv = ExtractedInvoice()
        inv.invoice_number = num
        inv.field_meta["invoice_number"] = _meta(sheet.name, f"rows {items[0][0]}-{items[-1][0]}")
        for i, row in items:
            get = lambda f: row[cols[f]] if f in cols and cols[f] < len(row) else None  # noqa: E731
            for f in ["invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency"]:
                _set(inv, f, get(f), sheet.name, f"row {i}")
            if ITEM_COLS & cols.keys() - {"description"} or "description" in cols:
                li = LineItem(description=None if get("description") is None else str(get("description")),
                              quantity=parse_amount(get("quantity")), unit_price=parse_amount(get("unit_price")),
                              amount=parse_amount(get("amount")))
                if li.description is not None or li.amount is not None:
                    inv.line_items.append(li)
        inv.source = {"sheet": sheet.name, "rows": [items[0][0], items[-1][0]]}
        out.append(_finish(inv, sheet.name))
    return out or None


def map_key_value(sheet: SheetTable) -> Optional[list[ExtractedInvoice]]:
    inv = ExtractedInvoice()
    item_cols, found = None, 0
    for i, row in enumerate(sheet.rows, start=1):
        keys = [(_key(c), j) for j, c in enumerate(row)]
        item_hits = {k: j for k, j in keys if k in ITEM_COLS}
        if len(item_hits) >= 2:
            item_cols = item_hits
            continue
        label = next(((k, j) for k, j in keys if k and k not in ITEM_COLS), None)
        if label and (item_cols is None or label[0] in ("total", "subtotal", "tax")):
            k, j = label
            val = next((c for c in row[j + 1:] if c is not None), None)
            if val is not None:
                _set(inv, k, val, sheet.name, f"row {i}"); found += 1
                if k == "total":
                    item_cols = None
            continue
        if item_cols:
            g = lambda f: row[item_cols[f]] if f in item_cols and item_cols[f] < len(row) else None  # noqa: E731
            li = LineItem(description=None if g("description") is None else str(g("description")),
                          quantity=parse_amount(g("quantity")), unit_price=parse_amount(g("unit_price")),
                          amount=parse_amount(g("amount")))
            if li.description is not None or li.amount is not None:
                inv.line_items.append(li)
    if found < 2 or inv.invoice_number is None and inv.total is None:
        return None
    inv.source = {"sheet": sheet.name}
    return [_finish(inv, sheet.name)]


def map_sheet(sheet: SheetTable) -> Optional[list[ExtractedInvoice]]:
    if sheet.n_rows < 2:
        return None
    return map_flat_table(sheet) or map_key_value(sheet)
