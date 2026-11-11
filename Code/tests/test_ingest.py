"""Tests for docintel.ingest. Run from the project folder:  pytest Code/tests -v

All test files are generated on the fly in a temporary folder, so the tests do not depend on the
downloaded datasets. OCR tests are skipped automatically when Tesseract is not installed.
"""
import csv
import shutil
import sys
from pathlib import Path

import pymupdf
import pytest
from openpyxl import Workbook
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader, PdfWriter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from docintel.ingest import ingest_file  # noqa: E402
from docintel.ingest.layout import words_to_text  # noqa: E402

HAS_TESSERACT = shutil.which("tesseract") is not None
needs_ocr = pytest.mark.skipif(not HAS_TESSERACT, reason="tesseract not installed")

INVOICE_LINES = ["INVOICE", "Invoice Number: 10253", "Customer Name: Hanari Carnes",
                 "Invoice Date: 2016-07-10", "Gorgonzola Telino   $10.00   20   $200.00",
                 "Maxilaku   $16.00   40   $640.00", "Total:   $840.00"]


def _font(size):
    for p in ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/TTF/DejaVuSans.ttf",
              "C:/Windows/Fonts/arial.ttf", "/System/Library/Fonts/Supplemental/Arial.ttf"]:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def make_invoice_image(path=None, size=(1700, 1100), skew=0.0):
    img = Image.new("L", size, 255)
    d = ImageDraw.Draw(img)
    f = _font(40)
    for i, line in enumerate(INVOICE_LINES):
        d.text((80, 80 + i * 110), line, fill=0, font=f)
    if skew:
        img = img.rotate(skew, expand=True, fillcolor=255)
    if path:
        img.save(path)
    return img


def make_digital_pdf(path, pages=1):
    doc = pymupdf.open()
    for p in range(pages):
        page = doc.new_page()
        for i, line in enumerate(INVOICE_LINES):
            page.insert_text((50, 72 + i * 20), line.replace("10253", str(10253 + p)), fontsize=11)
    doc.save(path)
    return path


# ---------------------------------------------------------------- digital PDF
def test_digital_pdf_uses_text_layer(tmp_path):
    r = ingest_file(make_digital_pdf(tmp_path / "a.pdf"))
    assert r.ok and r.detected_type == "pdf"
    assert r.page_count == 1 and r.pages[0].method == "pdf_text" and r.ocr_pages == 0
    assert "Invoice Number: 10253" in r.pages[0].text
    assert "$840.00" in r.full_text()


def test_multipage_pdf_and_progress(tmp_path):
    seen = []
    r = ingest_file(make_digital_pdf(tmp_path / "long.pdf", pages=25), progress=lambda d, t: seen.append((d, t)))
    assert r.ok and r.page_count == 25
    assert [p.page_no for p in r.pages] == list(range(1, 26))
    assert "10277" in r.pages[24].text
    assert seen[-1] == (25, 25)


def test_table_row_stays_on_one_line():
    words = [(50, 100, 120, 112, "Maxilaku", None), (300, 101, 350, 113, "$16.00", None),
             (420, 100, 440, 112, "40", None), (520, 99, 580, 111, "$640.00", None),
             (50, 130, 90, 142, "Total:", None), (520, 130, 580, 142, "$840.00", None)]
    lines = words_to_text(words).splitlines()
    assert lines[0].split() == ["Maxilaku", "$16.00", "40", "$640.00"]
    assert lines[1].split() == ["Total:", "$840.00"]


# ---------------------------------------------------------------- scanned PDF / images (OCR)
@needs_ocr
def test_scanned_pdf_is_ocrd(tmp_path):
    png = tmp_path / "scan.png"; make_invoice_image(png)
    doc = pymupdf.open(); doc.insert_pdf(pymupdf.open("pdf", pymupdf.open(png).convert_to_pdf())); doc.save(tmp_path / "scan.pdf")
    r = ingest_file(tmp_path / "scan.pdf", ocr="tesseract")
    assert r.ok and r.ocr_pages == 1 and r.pages[0].method == "ocr:tesseract"
    assert "10253" in r.pages[0].text and r.pages[0].ocr_confidence > 0.6


@needs_ocr
def test_mixed_pdf_digital_and_scanned_pages(tmp_path):
    make_digital_pdf(tmp_path / "d.pdf"); png = tmp_path / "s.png"; make_invoice_image(png)
    doc = pymupdf.open(); doc.insert_pdf(pymupdf.open(tmp_path / "d.pdf"))
    doc.insert_pdf(pymupdf.open("pdf", pymupdf.open(png).convert_to_pdf())); doc.save(tmp_path / "mixed.pdf")
    r = ingest_file(tmp_path / "mixed.pdf", ocr="tesseract")
    assert [p.method for p in r.pages] == ["pdf_text", "ocr:tesseract"]


@needs_ocr
@pytest.mark.parametrize("ext", ["png", "jpg"])
def test_image_ocr(tmp_path, ext):
    p = tmp_path / f"inv.{ext}"; make_invoice_image(p)
    r = ingest_file(p, ocr="tesseract")
    assert r.ok and r.detected_type == "image" and r.ocr_pages == 1
    assert "Hanari" in r.pages[0].text and "840.00" in r.pages[0].text


@needs_ocr
def test_skewed_low_resolution_image(tmp_path):
    img = make_invoice_image(skew=3.0).resize((900, 650))  # small and rotated
    p = tmp_path / "bad.jpg"; img.save(p, quality=50)
    r = ingest_file(p, ocr="tesseract")
    assert r.ok
    assert any("deskewed" in w for w in r.pages[0].warnings) and any("upscaled" in w for w in r.pages[0].warnings)
    assert "10253" in r.pages[0].text


@needs_ocr
def test_multiframe_tiff(tmp_path):
    a, b = make_invoice_image(), make_invoice_image()
    p = tmp_path / "two.tiff"; a.save(p, save_all=True, append_images=[b])
    r = ingest_file(p, ocr="tesseract")
    assert r.ok and r.detected_type == "tiff" and r.page_count == 2


# ---------------------------------------------------------------- spreadsheets
def test_xlsx_multiple_sheets_and_dates(tmp_path):
    from datetime import date
    wb = Workbook(); ws = wb.active; ws.title = "INV-1"
    ws.append(["Invoice Number", 10253]); ws.append(["Invoice Date", date(2016, 7, 10)]); ws.append([])
    ws.append(["Product", "Price", "Qty", "Total"]); ws.append(["Maxilaku", 16.0, 40, 640.0])
    ws2 = wb.create_sheet("INV-2"); ws2.append(["Invoice Number", 10254])
    wb.create_sheet("Empty")
    wb.save(tmp_path / "w.xlsx")
    r = ingest_file(tmp_path / "w.xlsx")
    assert r.ok and r.detected_type == "xlsx" and [s.name for s in r.sheets] == ["INV-1", "INV-2", "Empty"]
    s = r.sheets[0]
    assert s.rows[1] == ["Invoice Date", "2016-07-10", None, None]   # date cell -> ISO, padded row
    assert s.rows[-1] == ["Maxilaku", 16, 40, 640]                   # 16.0 -> 16, blank row dropped
    assert "EMPTY_SHEET" in r.sheets[2].warnings


def test_csv_cp1252_encoding(tmp_path):
    p = tmp_path / "a.csv"
    with open(p, "w", newline="", encoding="cp1252") as f:
        w = csv.writer(f); w.writerow(["invoice", "customer", "total"])
        w.writerow(["10253", "Comércio Mineiro", "840.00"]); w.writerow(["10254", "Königlich Essen", "12.50"])
    r = ingest_file(p)
    assert r.ok and r.detected_type == "csv"
    assert r.sheets[0].rows[1][1] == "Comércio Mineiro"


def test_csv_semicolon_delimiter(tmp_path):
    p = tmp_path / "b.csv"
    p.write_text("invoice;product;amount\n10253;Maxilaku;640,00\n10253;Gorgonzola;200,00\n", encoding="utf-8")
    r = ingest_file(p)
    assert r.ok and r.sheets[0].rows[1] == ["10253", "Maxilaku", "640,00"]
    assert "delimiter=;" in r.warnings


def test_csv_header_only_is_not_an_error(tmp_path):
    p = tmp_path / "h.csv"; p.write_text("invoice,total\n", encoding="utf-8")
    r = ingest_file(p)
    assert r.ok and any("NO_DATA_ROWS" in w for w in r.warnings)


# ---------------------------------------------------------------- failure scenarios
def test_empty_file(tmp_path):
    p = tmp_path / "empty.pdf"; p.write_bytes(b"")
    r = ingest_file(p)
    assert not r.ok and r.error_code == "EMPTY_FILE"


def test_truncated_pdf(tmp_path):
    good = make_digital_pdf(tmp_path / "g.pdf").read_bytes()
    p = tmp_path / "t.pdf"; p.write_bytes(good[: len(good) // 3])
    r = ingest_file(p)
    assert not r.ok and r.error_code == "CORRUPT_FILE"


def test_password_protected_pdf(tmp_path):
    make_digital_pdf(tmp_path / "g.pdf")
    w = PdfWriter(); [w.add_page(pg) for pg in PdfReader(str(tmp_path / "g.pdf")).pages]; w.encrypt("secret")
    with open(tmp_path / "locked.pdf", "wb") as f:
        w.write(f)
    r = ingest_file(tmp_path / "locked.pdf")
    assert not r.ok and r.error_code == "ENCRYPTED_PDF"


@pytest.mark.parametrize("name", ["junk.xlsx", "junk.tiff", "junk.png"])
def test_random_bytes_rejected(tmp_path, name):
    p = tmp_path / name; p.write_bytes(bytes(range(256)) * 16)
    r = ingest_file(p)
    assert not r.ok and r.error_code == "CORRUPT_FILE"


def test_truncated_image(tmp_path):
    p = tmp_path / "full.png"; make_invoice_image(p)
    q = tmp_path / "cut.png"; q.write_bytes(p.read_bytes()[:2000])
    r = ingest_file(q, ocr="tesseract")
    assert not r.ok and r.error_code == "CORRUPT_FILE"


def test_unsupported_type(tmp_path):
    p = tmp_path / "x.docx"
    import zipfile
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("word/document.xml", "<w/>")
    r = ingest_file(p)
    assert not r.ok and r.error_code in ("UNSUPPORTED_TYPE", "CORRUPT_FILE")


def test_wrong_extension_detected_by_content(tmp_path):
    p = tmp_path / "really_a_pdf.jpg"; make_digital_pdf(p)
    r = ingest_file(p)
    assert r.ok and r.detected_type == "pdf" and any("EXTENSION_MISMATCH" in w for w in r.warnings)


def test_missing_file_never_raises(tmp_path):
    r = ingest_file(tmp_path / "nope.pdf")
    assert not r.ok and r.error_code == "NOT_FOUND"


def test_result_is_json_serialisable(tmp_path):
    import json
    r = ingest_file(make_digital_pdf(tmp_path / "a.pdf"))
    json.dumps(r.to_dict())
    assert len(r.sha256) == 64
