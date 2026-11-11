"""Output structure of extraction (one ExtractedInvoice per invoice found in a document)."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

HEADER_FIELDS = ["invoice_number", "invoice_date", "supplier_name", "customer_name",
                 "subtotal", "tax", "total", "currency"]
AMOUNT_FIELDS = {"subtotal", "tax", "total"}

# status of each field:
#   found      - value present and found in the source text
#   missing    - not in the document (value is null)
#   uncertain  - value kept but low confidence or ambiguous (e.g. 03/04/2021) -> manual review
#   removed    - the model returned a value that is NOT in the source text; it was discarded (null)


@dataclass
class FieldMeta:
    status: str = "missing"
    confidence: Optional[float] = None
    evidence: Optional[str] = None
    raw: Optional[str] = None          # value exactly as written in the document
    note: Optional[str] = None


@dataclass
class LineItem:
    description: Optional[str] = None
    quantity: Optional[float] = None
    unit_price: Optional[float] = None
    amount: Optional[float] = None


@dataclass
class ExtractedInvoice:
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None      # ISO YYYY-MM-DD
    supplier_name: Optional[str] = None
    customer_name: Optional[str] = None
    subtotal: Optional[float] = None
    tax: Optional[float] = None
    total: Optional[float] = None
    currency: Optional[str] = None          # ISO 4217 code
    line_items: list[LineItem] = field(default_factory=list)
    field_meta: dict[str, FieldMeta] = field(default_factory=dict)
    source: dict[str, Any] = field(default_factory=dict)   # pages / sheet / rows this came from
    method: str = ""                        # llm:<model> | table_mapping | rules
    warnings: list[str] = field(default_factory=list)

    def values(self) -> dict:
        d = {k: getattr(self, k) for k in HEADER_FIELDS}
        d["line_items"] = [asdict(li) for li in self.line_items]
        return d

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ExtractedInvoice":
        d = dict(d)
        d["line_items"] = [LineItem(**li) for li in d.get("line_items", [])]
        d["field_meta"] = {k: FieldMeta(**v) for k, v in d.get("field_meta", {}).items()}
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
