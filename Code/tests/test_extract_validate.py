"""Tests for normalisation, extraction (rules, table mapping, LLM client with a fake server),
grounding, validation, duplicate detection and cross-verification.  Run:  pytest Code/tests -v
"""
import json
import sys
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from docintel.crossverify import cross_verify  # noqa: E402
from docintel.extract import extract_document  # noqa: E402
from docintel.extract.grounding import ground  # noqa: E402
from docintel.extract.llm import LLMClient, parse_json_object  # noqa: E402
from docintel.extract.rules import rules_extract  # noqa: E402
from docintel.extract.schema import ExtractedInvoice, LineItem  # noqa: E402
from docintel.extract.segment import split_units  # noqa: E402
from docintel.extract.tables import map_sheet  # noqa: E402
from docintel.ingest.types import IngestResult, PageText, SheetTable  # noqa: E402
from docintel.normalize import normalize_currency, parse_amount, parse_date  # noqa: E402
from docintel.validate import find_duplicates, review_status, validate_invoice  # noqa: E402

NORTHWIND = """INVOICE
Invoice Number: 10253
Customer Name: Hanari Carnes
Salesperson:   Janet Leverling
Invoice Date:   2016-07-10
Product Name   Unit Price   Quantity   Total Price
Gorgonzola Telino   $10.00   20   $200.00
Chartreuse verte   $14.40   42   $604.80
Maxilaku   $16.00   40   $640.00
Total:   $1444.80"""

KATANA = """Invoice no: 97159829
Date of issue:   09/18/2015
Seller:   Client:
Bradley-Andrade   Castro PLC
9879 Elizabeth Common   Unit 9678 Box 9664
ITEMS
No.   Description   Qty   UM   Net price   Net worth   VAT [%]   Gross worth
1.   12" Marble Lapis Inlay Chess   2,00   each   444,60   889,20   10%   978,12
SUMMARY
Total   $ 889,20   $ 88,92   $ 978,12"""


def doc(text, n=1):
    return IngestResult(path="x", file_name="x.pdf", size_bytes=1, sha256="abc", detected_type="pdf",
                        pages=[PageText(page_no=i + 1, text=t, method="pdf_text") for i, t in enumerate([text] * n if isinstance(text, str) else text)])


# ---------------------------------------------------------------- normalisation
@pytest.mark.parametrize("raw,val", [("$ 1 234,56", 1234.56), ("$1,444.80", 1444.8), ("RM 7.60", 7.6), ("889,20", 889.2),
                                     ("114 385,48", 114385.48), ("1.234.567", 1234567), ("(12.50)", -12.5), ("abc", None), (None, None)])
def test_parse_amount(raw, val):
    assert parse_amount(raw) == val


@pytest.mark.parametrize("raw,iso,ambiguous", [("2016-07-10", "2016-07-10", False), ("09/18/2015", "2015-09-18", False),
                                               ("22 MAR 2018", "2018-03-22", False), ("25/12/2018", "2018-12-25", False),
                                               ("03/04/2021", "2021-04-03", True), ("Mar 5, 2020", "2020-03-05", False)])
def test_parse_date(raw, iso, ambiguous):
    v, note = parse_date(raw)
    assert v == iso and (note is not None) == ambiguous


def test_currency():
    assert normalize_currency("$")[0] == "USD" and normalize_currency("RM")[0] == "MYR"
    assert normalize_currency("eur")[0] == "EUR" and normalize_currency("€")[0] == "EUR"
    assert normalize_currency(None) == (None, None)


# ---------------------------------------------------------------- grounding: no invented values
def test_grounding_discards_values_not_in_document():
    raw = {"invoice_number": {"value": "10253", "evidence": "Invoice Number: 10253", "confidence": 0.99},
           "supplier_name": {"value": "Northwind Traders", "evidence": None, "confidence": 0.8},   # not printed
           "tax": {"value": "0.00", "evidence": None, "confidence": 0.5},                          # invented
           "total": {"value": "$1444.80", "evidence": "Total: $1444.80", "confidence": 0.95},
           "line_items": [{"description": "Maxilaku", "quantity": "40", "unit_price": "16.00", "amount": "640.00"},
                          {"description": "Ghost item", "quantity": "1", "unit_price": "99.99", "amount": "99.99"}]}
    inv = ground(raw, NORTHWIND, "llm:test")
    assert inv.invoice_number == "10253" and inv.total == 1444.8
    assert inv.supplier_name is None and inv.field_meta["supplier_name"].status == "removed"
    assert inv.tax is None and inv.field_meta["tax"].status == "removed"
    assert inv.line_items[1].amount is None          # 99.99 is not on the page
    assert inv.field_meta["customer_name"].status == "missing"


def test_ambiguous_date_is_marked_uncertain():
    inv = ground({"invoice_date": {"value": "03/04/2021", "confidence": 0.9}}, "Date: 03/04/2021 Total RM 5.00", "llm:t")
    assert inv.invoice_date == "2021-04-03" and inv.field_meta["invoice_date"].status == "uncertain"


# ---------------------------------------------------------------- rules baseline
def test_rules_northwind():
    inv = ground(rules_extract(NORTHWIND), NORTHWIND, "rules")
    assert (inv.invoice_number, inv.invoice_date, inv.customer_name, inv.total) == ("10253", "2016-07-10", "Hanari Carnes", 1444.8)
    assert inv.supplier_name is None and inv.tax is None
    assert [(li.quantity, li.unit_price, li.amount) for li in inv.line_items] == [(20, 10.0, 200.0), (42, 14.4, 604.8), (40, 16.0, 640.0)]


def test_rules_katana_summary_row():
    inv = ground(rules_extract(KATANA), KATANA, "rules")
    assert inv.supplier_name == "Bradley-Andrade" and inv.customer_name == "Castro PLC"
    assert (inv.subtotal, inv.tax, inv.total, inv.currency) == (889.2, 88.92, 978.12, "USD")
    assert inv.invoice_date == "2015-09-18"


# ---------------------------------------------------------------- spreadsheets (no LLM)
def test_flat_table_grouped_by_invoice():
    rows = [["invoice_number", "invoice_date", "customer_name", "product", "unit_price", "quantity", "line_total", "invoice_total"],
            ["10250", "2016-07-08", "Hanari Carnes", "Clam Chowder", "7,70", 10, "77,00", "1813,00"],
            ["10250", "2016-07-08", "Hanari Carnes", "Dried Apples", "42,40", 35, "1484,00", "1813,00"],
            ["10251", "2016-07-09", "Victuailles", "Ravioli", "15.60", 15, "234.00", "234.00"]]
    invs = map_sheet(SheetTable(name="s", rows=rows, text="", n_rows=4, n_cols=8))
    assert [i.invoice_number for i in invs] == ["10250", "10251"]
    assert invs[0].total == 1813.0 and len(invs[0].line_items) == 2 and invs[0].line_items[1].amount == 1484.0
    assert invs[0].method == "table_mapping"


def test_key_value_sheet():
    rows = [["INVOICE", None, None, None], ["Invoice Number", 10253, None, None], ["Invoice Date", "2016-07-10", None, None],
            ["Customer Name", "Hanari Carnes", None, None], ["Product Name", "Unit Price", "Quantity", "Total Price"],
            ["Maxilaku", 16, 40, 640], ["Gorgonzola", 10, 20, 200], [None, None, "Total:", 840]]
    inv = map_sheet(SheetTable(name="Invoice", rows=rows, text="", n_rows=8, n_cols=4))[0]
    assert (inv.invoice_number, inv.invoice_date, inv.customer_name, inv.total) == ("10253", "2016-07-10", "Hanari Carnes", 840.0)
    assert [li.amount for li in inv.line_items] == [640.0, 200.0]


# ---------------------------------------------------------------- segmentation
def test_split_pdf_into_invoices():
    pages = [NORTHWIND, NORTHWIND.replace("10253", "10254"), "continued page without a number\nmore items"]
    units = split_units(doc(pages))
    assert [u.source["pages"] for u in units] == [[1], [2, 3]]


# ---------------------------------------------------------------- LLM client against a fake OpenAI-compatible server
class _Fake(BaseHTTPRequestHandler):
    mode = "ok"

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        assert body["temperature"] == 0 and body["messages"][0]["role"] == "system"
        if _Fake.mode == "error":
            self.send_response(500); self.end_headers(); self.wfile.write(b"boom"); return
        answer = {"invoice_number": {"value": "10253", "evidence": "Invoice Number: 10253", "confidence": 0.98},
                  "invoice_date": {"value": "2016-07-10", "evidence": "Invoice Date: 2016-07-10", "confidence": 0.97},
                  "supplier_name": {"value": None, "evidence": None, "confidence": 0.9},
                  "customer_name": {"value": "Hanari Carnes", "evidence": "Customer Name: Hanari Carnes", "confidence": 0.97},
                  "subtotal": {"value": None}, "tax": {"value": None},
                  "total": {"value": "$1444.80", "evidence": "Total: $1444.80", "confidence": 0.98},
                  "currency": {"value": "$", "evidence": "$1444.80", "confidence": 0.9},
                  "line_items": [{"description": "Maxilaku", "quantity": "40", "unit_price": "$16.00", "amount": "$640.00"}]}
        content = "```json\n" + json.dumps(answer) + "\n```"   # fenced on purpose
        out = {"model": "fake", "choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
        self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps(out).encode())

    def log_message(self, *a):
        pass


@pytest.fixture()
def fake_llm():
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()
    yield LLMClient(provider="openai", model="fake", base_url=f"http://127.0.0.1:{srv.server_port}/v1", max_retries=1)
    srv.shutdown()


def test_llm_extraction_end_to_end(fake_llm):
    _Fake.mode = "ok"
    invs, info = extract_document(doc(NORTHWIND), fake_llm)
    inv = invs[0]
    assert inv.method == "llm:fake" and info["input_tokens"] == 100
    assert (inv.invoice_number, inv.total, inv.currency, inv.supplier_name) == ("10253", 1444.8, "USD", None)
    assert inv.line_items[0].amount == 640.0


def test_llm_failure_falls_back_to_rules(fake_llm):
    _Fake.mode = "error"
    invs, info = extract_document(doc(NORTHWIND), fake_llm)
    assert invs[0].method == "rules" and invs[0].total == 1444.8
    assert any("LLM failed" in w for w in invs[0].warnings)
    _Fake.mode = "ok"


def test_parse_json_object_tolerates_prose():
    assert parse_json_object('Here you go:\n{"a": "x}", "b": [1, 2]} hope it helps') == {"a": "x}", "b": [1, 2]}


# ---------------------------------------------------------------- validation
def inv(**kw):
    base = dict(invoice_number="1", invoice_date="2020-01-01", supplier_name="ACME", total=110.0)
    base.update(kw)
    return ExtractedInvoice(**base)


def rules_of(i):
    return {x["rule"] for x in validate_invoice(i, today=date(2026, 1, 1))}


def test_clean_invoice_is_valid():
    i = inv(subtotal=100.0, tax=10.0, line_items=[LineItem("a", 2, 25.0, 50.0), LineItem("b", 1, 50.0, 50.0)])
    assert rules_of(i) == set() and review_status(validate_invoice(i, today=date(2026, 1, 1))) == "valid"


def test_missing_fields_flagged():
    assert {"MISSING_FIELD"} <= rules_of(inv(supplier_name=None, invoice_number=None))


def test_line_item_math_and_sums():
    i = inv(subtotal=100.0, tax=10.0, line_items=[LineItem("a", 2, 25.0, 60.0), LineItem("b", 1, 50.0, 50.0)])
    assert {"LINE_ITEM_MATH", "LINE_SUM_MISMATCH"} <= rules_of(i)


def test_total_mismatch_and_tax_rate():
    assert "TOTAL_MISMATCH" in rules_of(inv(subtotal=100.0, tax=10.0, total=120.0))
    assert "TAX_RATE_IMPLAUSIBLE" in rules_of(inv(subtotal=100.0, tax=50.0, total=150.0))


def test_lines_vs_total_when_no_tax():
    assert "LINE_SUM_MISMATCH" in rules_of(inv(total=500.0, line_items=[LineItem("a", 1, 100.0, 100.0)]))


def test_dates_and_negative_amounts():
    assert "FUTURE_DATE" in rules_of(inv(invoice_date="2030-01-01"))
    assert "NEGATIVE_AMOUNT" in rules_of(inv(total=-5.0))


def test_duplicates_across_files_not_within_one_file():
    a, b, c = inv(invoice_number="77"), inv(invoice_number="78"), inv(invoice_number="77")
    d = find_duplicates([("f1#0", a, "h1"), ("f1#1", b, "h1"), ("f2#0", c, "h2")])
    assert "f1#1" not in d                       # same file hash, different invoice in the same workbook
    assert d["f2#0"][0]["duplicate_of"] == "f1#0"


def test_cross_verify():
    pdf = [inv(invoice_number="10", total=100.0, line_items=[LineItem("x", 2, 50.0, 100.0)]), inv(invoice_number="11")]
    led = [inv(invoice_number="10", total=150.0, line_items=[LineItem("x", 3, 50.0, 150.0)])]
    r = cross_verify(pdf, led, "pdf", "ledger")
    fields = {m["field"] for m in r["mismatches"]}
    assert "total" in fields and "line_items[0].quantity" in fields
    assert r["only_in_pdf"] == ["11"] and not r["consistent"]
