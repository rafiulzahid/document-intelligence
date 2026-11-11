#!/usr/bin/env python3
"""
prepare_dataset.py - build the labeled evaluation set for the Document Intelligence System.

Default folder layout (project root = the folder that contains Code/):
    <root>/Code/prepare_dataset.py                 this script
    <root>/northwind_invoices/                     Northwind invoice_*.pdf (any sub-folder)
    <root>/scanned_receipts/                       SROIE from Voxel51: samples.json + data/*.jpg
    <root>/Samples of electronic invoices/         Mendeley e-invoice PDFs (optional; .zip is unpacked)
    <root>/katana_invoices/                        Katana images + labels (downloaded automatically)

Writes <root>/data/eval/:
    files/<category>/<doc_id>.<ext>   documents to feed into the pipeline
    gt/<doc_id>.json                  ground truth in one unified format
    manifest.csv                      one row per document
    DATASET_CARD.md                   counts, sources, licenses, exclusions

Run (inside a virtual environment):
    pip install pymupdf pypdf openpyxl pandas python-dateutil pillow
    python Code/prepare_dataset.py            # uses the default folders above
    python Code/prepare_dataset.py --help     # to point at other folders

Every number written to the ground truth is copied from source labels or read from the
generated file itself. Nothing is estimated.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import pymupdf  # PyMuPDF >= 1.24.3 (older versions: `import fitz as pymupdf`)
from openpyxl import Workbook
from PIL import Image, ImageFilter
from pypdf import PdfReader, PdfWriter

FIELDS = ["invoice_number", "invoice_date", "supplier_name", "customer_name",
          "subtotal", "tax", "total", "currency", "line_items"]
TOL = 0.05  # tolerance used only to decide whether a source label is internally consistent


# ----------------------------------------------------------------------------- helpers
def parse_amount(value):
    """'$ 1 234,56' -> 1234.56, '$1444.80' -> 1444.8, 'RM 7.60' -> 7.6. Returns None if no number."""
    if value is None:
        return None
    s = str(value).strip()
    s = re.sub(r"[^\d,.\-]", "", s)  # drop currency symbols, letters and spaces (space = thousands sep)
    if not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):          # 1.234,56
            s = s.replace(".", "").replace(",", ".")
        else:                                     # 1,234.56
            s = s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if re.search(r",\d{1,2}$", s) else s.replace(",", "")
    elif s.count(".") > 1:                        # 1.234.567
        s = s.replace(".", "")
    try:
        return round(float(s), 2)
    except ValueError:
        return None


SROIE_DATE_FORMATS = ["%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y", "%d.%m.%y", "%d.%m.%Y",
                      "%d %b %Y", "%d-%b-%Y", "%d/%b/%Y", "%d %b %y", "%Y-%m-%d", "%Y/%m/%d", "%b %d, %Y"]


def parse_date(raw, formats):
    if not raw:
        return None
    s = re.sub(r"\s+", " ", str(raw).strip().strip("()"))
    for f in formats:  # strptime month names are case-insensitive
        try:
            d = datetime.strptime(s, f).date()
        except ValueError:
            continue
        if 1990 <= d.year <= 2035:
            return d.isoformat()
    return None


def close(a, b, tol=TOL):
    return a is not None and b is not None and abs(a - b) <= tol


def empty_invoice():
    return {k: (None if k != "line_items" else []) for k in FIELDS}


def write_gt(out, doc):
    (out / "gt").mkdir(parents=True, exist_ok=True)
    with open(out / "gt" / f"{doc['doc_id']}.json", "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, ensure_ascii=False)


def make_doc(doc_id, source, category, file_rel, file_type, invoices, labeled,
             outcome="success", flags=None, notes=None, **extra):
    d = {"doc_id": doc_id, "source": source, "category": category, "file": file_rel,
         "file_type": file_type, "expected_outcome": outcome, "expected_flags": flags or [],
         "labeled_fields": labeled, "invoices": invoices, "notes": notes or ""}
    d.update(extra)
    return d


# ----------------------------------------------------------------------------- Katana
def load_katana_label(raw_json: str):
    """Normalise the 5 label shapes found in invoices-donut-data-v1 into header/items/summary."""
    d = json.loads(raw_json)
    g = d.get("gt_parse", d)
    if set(g.keys()) == {"None"}:
        g = g["None"]
    elif "None" in g:
        g = {k: v for k, v in g.items() if k != "None"}
    if "header" in g or "items" in g or "summary" in g:
        header, items, summary = g.get("header", {}) or {}, g.get("items", []) or [], g.get("summary", {}) or {}
        flat = False
    else:  # flattened row: header keys, ONE item and summary keys all at top level
        header = {k: g[k] for k in ["invoice_no", "invoice_date", "seller", "client"] if k in g}
        items = [{k: g[k] for k in g if k.startswith("item_")}] if any(k.startswith("item_") for k in g) else []
        summary = {k: g[k] for k in g if k.startswith("total_")}
        flat = True
    if isinstance(items, dict):
        items = [items]
    return header, items, summary, flat


def katana_invoice(raw_json: str):
    header, items, summary, flat = load_katana_label(raw_json)
    inv = empty_invoice()
    labeled, notes = [], []

    if header.get("invoice_no"):
        inv["invoice_number"] = str(header["invoice_no"]).strip(); labeled.append("invoice_number")
    d = parse_date(header.get("invoice_date"), ["%m/%d/%Y"])
    if d:
        inv["invoice_date"] = d; labeled.append("invoice_date")
    # a few source labels contain a tax id instead of a name: leave those unscored
    if header.get("seller") and re.search(r"[A-Za-z]{2,}", header["seller"]):
        inv["supplier_name"] = header["seller"].strip(); labeled.append("supplier_name")
    if header.get("client") and re.search(r"[A-Za-z]{2,}", header["client"]):
        inv["customer_name"] = header["client"].strip(); labeled.append("customer_name")

    sub, tax, tot = (parse_amount(summary.get(k)) for k in ["total_net_worth", "total_vat", "total_gross_worth"])
    if sub is not None and tax is not None and tot is not None and close(sub + tax, tot):
        inv.update(subtotal=sub, tax=tax, total=tot)
        labeled += ["subtotal", "tax", "total"]
        if "$" in json.dumps(summary):
            inv["currency"] = "USD"; labeled.append("currency")
    else:
        notes.append(f"summary not usable ({summary}); subtotal/tax/total not labeled")

    li = []
    for it in items:
        li.append({"description": (it.get("item_desc") or "").strip() or None,
                   "quantity": parse_amount(it.get("item_qty")),
                   "unit_price": parse_amount(it.get("item_net_price")),
                   "amount": parse_amount(it.get("item_net_worth")),
                   "tax_rate": it.get("item_vat") if str(it.get("item_vat", "")).endswith("%") else None,
                   "gross_amount": parse_amount(it.get("item_gross_worth"))})
    inv["line_items"] = li
    amounts = [x["amount"] for x in li]
    if li and not flat and None not in amounts and inv["subtotal"] is not None \
            and close(sum(amounts), inv["subtotal"], 0.05 * max(1, len(li))):
        labeled.append("line_items")
    else:
        notes.append("line items incomplete or inconsistent with subtotal; line_items not labeled")
    return inv, labeled, "; ".join(notes)


# ----------------------------------------------------------------------------- Northwind
AMT = re.compile(r"^\$[\d,]+\.\d{2}$")


def pdf_rows(doc):
    """Words grouped into visual rows by position (robust to text-stream order)."""
    rows = []
    for page in doc:
        words = sorted(page.get_text("words"), key=lambda w: (round(w[3]), w[0]))
        cur, y = [], None
        for w in words:
            if y is not None and abs(w[3] - y) > 3:
                rows.append(sorted(cur, key=lambda t: t[0])); cur = []
            cur.append(w); y = w[3]
        if cur:
            rows.append(sorted(cur, key=lambda t: t[0]))
    return [[w[4] for w in r] for r in rows]


def northwind_parse(pdf_path: Path):
    """Ground truth from the clean text layer. Returns (invoice, labeled, notes, extra)."""
    doc = pymupdf.open(pdf_path)
    rows = pdf_rows(doc)
    inv = empty_invoice()
    salesperson, total, items, in_table = None, None, [], False
    for toks in rows:
        line = " ".join(toks)
        for label, key in [("Invoice Number:", "invoice_number"), ("Customer Name:", "customer_name"),
                           ("Invoice Date:", "invoice_date"), ("Salesperson:", "salesperson")]:
            if line.startswith(label):
                val = line[len(label):].strip()
                if key == "salesperson":
                    salesperson = val
                else:
                    inv[key] = val
        if line.startswith("Product Name"):
            in_table = True; continue
        if toks and toks[0] == "Total:":
            total = parse_amount(toks[-1]); in_table = False; continue
        if in_table and len(toks) >= 4 and AMT.match(toks[-3]) and toks[-2].isdigit() and AMT.match(toks[-1]):
            items.append({"description": " ".join(toks[:-3]), "quantity": float(toks[-2]),
                          "unit_price": parse_amount(toks[-3]), "amount": parse_amount(toks[-1])})
    inv.update(total=total, currency="USD", line_items=items)
    # Not printed on these invoices -> correct answer is null (tests "no invented values")
    labeled = ["invoice_number", "invoice_date", "supplier_name", "customer_name",
               "subtotal", "tax", "total", "currency", "line_items"]
    notes = []
    if not items or total is None or not close(sum(x["amount"] for x in items), total, 0.02):
        notes.append("line items do not sum to total in source PDF")
    return inv, labeled, "; ".join(notes), {"salesperson": salesperson, "pages": len(doc)}


def replace_text_on_pdf(src: Path, dst: Path, old: str, new: str, row_hint: str | None = None):
    """Replace one printed amount in a digital PDF (redact + overwrite). row_hint picks the row."""
    doc = pymupdf.open(src)
    page = doc[0]
    rects = page.search_for(old)
    if row_hint:
        hint = page.search_for(row_hint)
        if hint:
            rects = [r for r in rects if abs(r.y0 - hint[0].y0) < 3] or rects
    if not rects:
        raise ValueError(f"{old} not found in {src}")
    r = rects[-1]
    size = 12
    for b in page.get_text("dict")["blocks"]:
        for l in b.get("lines", []):
            for s in l["spans"]:
                if s["text"].strip() == old:
                    size = s["size"]
    page.add_redact_annot(r, fill=(1, 1, 1))
    page.apply_redactions()
    page.insert_text((r.x0, r.y1 - 0.22 * size), new, fontsize=size, fontname="helv")
    doc.save(dst)



HF_ROWS = "https://datasets-server.huggingface.co/rows?dataset=katanaml-org/invoices-donut-data-v1&config=default&split={split}&offset={off}&length=100"
KATANA_SIZES = {"train": 425, "validation": 50, "test": 26}


def _get_json(url, tries=4):
    for t in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return json.load(r)
        except Exception as e:  # noqa: BLE001
            if t == tries - 1:
                raise
            print(f"  retry ({type(e).__name__})"); time.sleep(3 * (t + 1))


def fetch_katana(dest: Path, splits, with_images=True):
    """Download Katana labels (and images) from the Hugging Face dataset viewer API. Cached on disk."""
    dest.mkdir(parents=True, exist_ok=True)
    for split in splits:
        have = len(list(dest.glob(f"{split}_*.json")))
        if have >= KATANA_SIZES[split] and (not with_images or len(list(dest.glob(f"{split}_*.png"))) >= have):
            continue
        print(f"  downloading Katana {split} ({KATANA_SIZES[split]} rows)...")
        for off in range(0, KATANA_SIZES[split], 100):
            for r in _get_json(HF_ROWS.format(split=split, off=off))["rows"]:
                stem = dest / f"{split}_{r['row_idx']:03d}"
                stem.with_suffix(".json").write_text(r["row"]["ground_truth"], encoding="utf-8")
                if with_images and not stem.with_suffix(".png").exists():
                    tmp = stem.with_suffix(".download")
                    urllib.request.urlretrieve(r["row"]["image"]["src"], tmp)
                    Image.open(tmp).convert("RGB").save(stem.with_suffix(".png"))
                    tmp.unlink()


def mendeley_variant(name: str) -> str:
    if "_charspace_" in name:
        return "charspace_iban"
    if "_color_" in name:
        return "colored_iban"
    return "valid"


def mendeley_id(name: str):
    m = re.match(r"invoice_(\d+)", name)
    return m.group(1) if m else None



IMG_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
SROIE_LABELS_URL = "https://huggingface.co/datasets/Voxel51/scanned_receipts/resolve/main/samples.json"
SROIE_IMG_URL = "https://huggingface.co/datasets/Voxel51/scanned_receipts/resolve/main/"


def _norm_keys(d):
    return {str(k).strip().lower(): v for k, v in d.items()}


def find_sroie_samples(sroie_dir: Path):
    """Return ([{image, company, date, address, total}], format_name) for the common SROIE layouts."""
    if not sroie_dir.exists():
        return [], "missing"
    images = {}
    for q in sroie_dir.rglob("*"):
        if q.is_file() and q.suffix.lower() in IMG_EXT:
            images.setdefault(q.stem, q)
    # 1) Voxel51 / FiftyOne export: samples.json with filepath + fields (matched to images by file name)
    for sj in sorted(q for q in sroie_dir.rglob("*.json") if q.name.lower() in {"samples.json", "sample.json"}):
        try:
            data = json.load(open(sj, encoding="utf-8"))
        except Exception:  # noqa: BLE001  (e.g. a Git LFS pointer file)
            continue
        out = []
        for x in data.get("samples", []):
            img = sj.parent / x.get("filepath", "")
            if not img.is_file():
                img = images.get(Path(x.get("filepath", "")).stem)
            if img is not None and img.is_file():
                out.append({"image": img, "company": x.get("company"), "date": x.get("date"),
                            "address": x.get("address"), "total": x.get("total")})
        if out:
            return out, f"Voxel51 samples.json at {sj.relative_to(sroie_dir)}"
    # 2) ICDAR 2019 / Kaggle / GitHub layouts: one label file per image (key/*.json or task2 *.txt)
    out = []
    for q in sroie_dir.rglob("*"):
        if not q.is_file() or q.suffix.lower() not in {".json", ".txt"} or q.stem not in images:
            continue
        try:
            txt = q.read_text(encoding="utf-8", errors="replace").strip()
            if not txt.startswith("{"):
                continue
            lab = _norm_keys(json.loads(txt))
        except Exception:  # noqa: BLE001
            continue
        if "total" in lab or "company" in lab:
            out.append({"image": images[q.stem], "company": lab.get("company"), "date": lab.get("date"),
                        "address": lab.get("address"), "total": lab.get("total")})
    if out:
        return out, "one label file per image"
    # 3) a CSV / Parquet table with file name + company/date/total columns
    import pandas as pd
    for t in sorted(list(sroie_dir.rglob("*.csv")) + list(sroie_dir.rglob("*.parquet"))):
        try:
            df = pd.read_parquet(t) if t.suffix == ".parquet" else pd.read_csv(t)
        except Exception:  # noqa: BLE001
            continue
        cols = {c.lower(): c for c in df.columns}
        if "total" not in cols and "company" not in cols:
            continue
        tmp = Path(tempfile.mkdtemp(prefix="sroie_"))
        for i, row in df.iterrows():
            img = None
            for key in ["filepath", "file", "filename", "file_name", "image_path", "image_id", "id"]:
                if key in cols and isinstance(row[cols[key]], str) and Path(row[cols[key]]).stem in images:
                    img = images[Path(row[cols[key]]).stem]; break
            if img is None and "image" in cols and isinstance(row[cols["image"]], dict) and row[cols["image"]].get("bytes"):
                img = tmp / f"{t.stem}_{i:05d}.jpg"; img.write_bytes(row[cols["image"]]["bytes"])
            if img is None:
                continue
            g = lambda k: row[cols[k]] if k in cols else None  # noqa: E731
            out.append({"image": img, "company": g("company"), "date": g("date"),
                        "address": g("address"), "total": g("total")})
        if out:
            return out, f"table {t.name}"
    return [], "not recognised"


# ----------------------------------------------------------------------------- generators
def degrade_image(img: Image.Image, rng: random.Random) -> Image.Image:
    img = img.convert("L")
    img = img.rotate(rng.uniform(-3, 3), expand=True, fillcolor=255, resample=Image.BICUBIC)
    img = img.filter(ImageFilter.GaussianBlur(rng.uniform(0.6, 1.2)))
    noise = Image.effect_noise(img.size, rng.uniform(18, 30))
    return Image.blend(img, noise, 0.12)


def write_invoice_sheet(ws, inv, extra, as_date_cell):
    ws.append(["INVOICE"]); ws.append([])
    ws.append(["Invoice Number", int(inv["invoice_number"]) if inv["invoice_number"].isdigit() else inv["invoice_number"]])
    ws.append(["Invoice Date", date.fromisoformat(inv["invoice_date"]) if as_date_cell else inv["invoice_date"]])
    ws.append(["Customer Name", inv["customer_name"]])
    ws.append(["Salesperson", extra.get("salesperson")]); ws.append([])
    ws.append(["Product Name", "Unit Price", "Quantity", "Total Price"])
    for it in inv["line_items"]:
        ws.append([it["description"], it["unit_price"], int(it["quantity"]), it["amount"]])
    ws.append([]); ws.append(["", "", "Total:", inv["total"]])
    if as_date_cell:
        ws["B4"].number_format = "yyyy-mm-dd"


def sheet_gt(inv):
    """Spreadsheets carry no currency symbol, so the correct answer for currency is null."""
    g = json.loads(json.dumps(inv)); g["currency"] = None
    return g


def flat_rows(inv, decimal_comma=False):
    f = (lambda x: f"{x:.2f}".replace(".", ",")) if decimal_comma else (lambda x: f"{x:.2f}")
    for it in inv["line_items"]:
        yield [inv["invoice_number"], inv["invoice_date"], inv["customer_name"], it["description"],
               f(it["unit_price"]), int(it["quantity"]), f(it["amount"]), f(inv["total"])]


CSV_HEADER = ["invoice_number", "invoice_date", "customer_name", "product", "unit_price",
              "quantity", "line_total", "invoice_total"]


# ----------------------------------------------------------------------------- main
def main():
    root = Path(__file__).resolve().parent.parent  # folder that contains Code/
    ap = argparse.ArgumentParser(description="Build the labeled evaluation set.")
    ap.add_argument("--root", type=Path, default=root, help="project folder (default: parent of Code/)")
    ap.add_argument("--northwind", type=Path, help="default: <root>/northwind_invoices")
    ap.add_argument("--sroie", type=Path, help="default: <root>/scanned_receipts")
    ap.add_argument("--mendeley", type=Path, help="default: <root>/Samples of electronic invoices")
    ap.add_argument("--katana", type=Path, help="default: <root>/katana_invoices (auto-downloaded)")
    ap.add_argument("--out", type=Path, help="default: <root>/data/eval")
    ap.add_argument("--no-download", action="store_true", help="do not download Katana from Hugging Face")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--n-northwind", type=int, default=50)
    ap.add_argument("--n-sroie", type=int, default=30)
    ap.add_argument("--n-mendeley", type=int, default=30)
    ap.add_argument("--n-sheets", type=int, default=20)
    ap.add_argument("--n-degraded", type=int, default=10)
    ap.add_argument("--n-planted", type=int, default=10)
    ap.add_argument("--katana-splits", default="validation,test")
    ap.add_argument("--no-long-pdf", action="store_true")
    a = ap.parse_args()
    R = a.root
    nw_dir = a.northwind or R / "northwind_invoices"
    sroie_dir = a.sroie or R / "scanned_receipts"
    mend_dir = a.mendeley or R / "Samples of electronic invoices"
    k_dir = a.katana or R / "katana_invoices"
    out = a.out or R / "data" / "eval"
    print(f"project root : {R}\nnorthwind    : {nw_dir}\nsroie        : {sroie_dir}\n"
          f"mendeley     : {mend_dir}\nkatana       : {k_dir}\noutput       : {out}\n")
    for name, d in [("northwind", nw_dir), ("sroie", sroie_dir)]:
        if not d.exists():
            print(f"WARNING: {name} folder not found: {d}")

    rng = random.Random(a.seed)
    if out.exists():
        shutil.rmtree(out)
    (out / "files").mkdir(parents=True)
    docs = []
    excluded = Counter()

    def place(category, name, src=None, data=None):
        d = out / "files" / category
        d.mkdir(parents=True, exist_ok=True)
        p = d / name
        if src is not None:
            shutil.copyfile(src, p)
        elif data is not None:
            p.write_bytes(data)
        return p, str(p.relative_to(out))

    # ---------------- Katana (labels + images; downloaded once if missing)
    splits = a.katana_splits.split(",")
    if not a.no_download:
        try:
            fetch_katana(k_dir, splits, with_images=True)
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: could not download Katana ({type(e).__name__}: {e}); continuing without it")
    k_files = sorted(p for s_ in splits for p in k_dir.glob(f"{s_}_*.json"))
    for j in k_files:
        img = j.with_suffix(".png")
        if not img.exists():
            excluded["katana: image missing"] += 1; continue
        inv, labeled, notes = katana_invoice(j.read_text(encoding="utf-8"))
        if "line_items" not in labeled: excluded["katana: line_items unlabeled"] += 1
        if "total" not in labeled: excluded["katana: totals unlabeled"] += 1
        doc_id = f"katana_{j.stem}"
        _, rel = place("katana", doc_id + ".png", src=img)
        docs.append(make_doc(doc_id, "katana", "image_ocr", rel, "png", [inv], labeled, notes=notes))
    print(f"katana: {sum(d['source'] == 'katana' for d in docs)} docs")

    # ---------------- Mendeley e-invoices (the PDFs the Katana images were made from)
    mend_pdfs = []
    if mend_dir.exists():
        mend_pdfs = sorted(mend_dir.rglob("*.pdf"))
    zips = sorted(R.glob("Samples of electronic invoices*.zip")) if not mend_pdfs else []
    if zips:
        tmpdir = Path(tempfile.mkdtemp(prefix="mendeley_"))
        print(f"  unpacking {zips[0].name} ...")
        with zipfile.ZipFile(zips[0]) as z:
            z.extractall(tmpdir)
        for inner in list(tmpdir.rglob("*.zip")):  # Mendeley "download all" zips each folder again
            with zipfile.ZipFile(inner) as z:
                z.extractall(inner.with_suffix(""))
        mend_pdfs = sorted(tmpdir.rglob("*.pdf"))
    if mend_pdfs:
        all_labels = {}
        if not a.no_download:
            try:
                fetch_katana(k_dir / "_all_labels", ["train", "validation", "test"], with_images=False)
            except Exception as e:  # noqa: BLE001
                print(f"WARNING: could not download Katana labels for Mendeley matching ({e})")
        for j in list((k_dir / "_all_labels").glob("*.json")) + k_files:
            raw_label = j.read_text(encoding="utf-8")
            try:
                hdr, _, _, _ = load_katana_label(raw_label)
            except Exception:  # noqa: BLE001
                continue
            if hdr.get("invoice_no"):
                all_labels[str(hdr["invoice_no"]).strip()] = (raw_label, j.stem)
        stats = Counter(); matched = []
        for p in mend_pdfs:
            stats["pdf files"] += 1
            try:
                text = "".join(pg.get_text() for pg in pymupdf.open(p))
            except Exception:  # noqa: BLE001
                stats["unreadable"] += 1; continue
            if len(text.strip()) < 30:
                stats["no text layer (image-only)"] += 1; continue
            stats["with text layer"] += 1
            hits = [n for n in set(re.findall(r"\d{6,}", text)) if n in all_labels]
            if len(hits) != 1:
                continue
            raw_label, stem = all_labels[hits[0]]
            inv, labeled, notes = katana_invoice(raw_label)
            t = inv.get("total")
            if t is not None and f"{t:.2f}".replace(".", ",") not in text.replace(" ", "") \
                    and f"{t:.2f}" not in text.replace(" ", ""):
                stats["number matched but total not found (skipped)"] += 1; continue
            stats["matched to a Katana label"] += 1
            matched.append((p, inv, labeled, notes, stem))
        print("mendeley:", dict(stats))
        by_variant = {}
        for m in matched:
            by_variant.setdefault(mendeley_variant(m[0].name), []).append(m)
        quota = {"valid": a.n_mendeley, "colored_iban": max(1, a.n_mendeley // 6), "charspace_iban": max(1, a.n_mendeley // 6)}
        for variant, items in by_variant.items():
            rng.shuffle(items)
            for p, inv, labeled, notes, stem in items[: quota.get(variant, 0)]:
                doc_id = f"mendeley_{variant}_{mendeley_id(p.name) or p.stem}"
                _, rel = place("mendeley_pdf", doc_id + ".pdf", src=p)
                docs.append(make_doc(doc_id, "mendeley+katana_labels", "digital_pdf" if variant == "valid" else "pdf_variant",
                                     rel, "pdf", [inv], labeled,
                                     notes=f"Mendeley PDF ({variant}); labels from Katana row {stem}; {notes}".strip("; ")))
        print(f"mendeley: {sum(d['source'].startswith('mendeley') for d in docs)} docs")
        if not matched:
            print("  NOTE: no Mendeley PDF could be matched to a Katana label. Katana images are still used.")
    else:
        print("mendeley: no PDFs found (optional), skipped")

    # ---------------- Northwind: parse all once
    nw_paths = sorted(p for p in nw_dir.rglob("invoice_*.pdf") if re.match(r"invoice_\d+\.pdf$", p.name))
    print(f"northwind: {len(nw_paths)} PDFs found")
    nw = {}
    for p in nw_paths:
        try:
            inv, labeled, notes, extra = northwind_parse(p)
            if notes:
                excluded["northwind: source inconsistent"] += 1; continue
            nw[p] = (inv, labeled, extra)
        except Exception as e:  # noqa: BLE001
            excluded[f"northwind: parse error {type(e).__name__}"] += 1
    pool = list(nw)
    rng.shuffle(pool)
    need = a.n_northwind + a.n_sheets + 3 + a.n_planted
    if len(pool) < need:
        print(f"WARNING: only {len(pool)} usable Northwind PDFs, need {need}; counts will be reduced")
    take = lambda n: [pool.pop() for _ in range(min(n, len(pool)))]  # noqa: E731
    nw_eval, nw_sheet, nw_multi, nw_plant = take(a.n_northwind), take(a.n_sheets), take(3), take(a.n_planted)

    for p in nw_eval:
        inv, labeled, extra = nw[p]
        doc_id = f"northwind_{inv['invoice_number']}"
        _, rel = place("northwind_pdf", doc_id + ".pdf", src=p)
        docs.append(make_doc(doc_id, "northwind", "digital_pdf", rel, "pdf", [inv], labeled,
                             notes="supplier_name, subtotal and tax are not printed: correct output is null"))

    # Degraded "scans" of the eval PDFs
    for p in nw_eval[:a.n_degraded]:
        inv, labeled, _ = nw[p]
        pix = pymupdf.open(p)[0].get_pixmap(dpi=rng.choice([150, 170, 200]))
        img = degrade_image(Image.frombytes("RGB", (pix.width, pix.height), pix.samples), rng)
        doc_id = f"degraded_{inv['invoice_number']}"
        path, rel = place("degraded_scan", doc_id + ".jpg")
        img.save(path, "JPEG", quality=40)
        docs.append(make_doc(doc_id, "northwind", "degraded_scan", rel, "jpg", [inv], labeled,
                             notes="rendered from the digital PDF, rotated, blurred, noise, JPEG q40"))

    # ---------------- Spreadsheets
    sheet_items = list(nw_sheet)
    n_xlsx = max(0, len(sheet_items) // 2 - 1)
    for k, p in enumerate(sheet_items[:n_xlsx]):
        inv, labeled, extra = nw[p]
        wb = Workbook(); ws = wb.active; ws.title = "Invoice"
        write_invoice_sheet(ws, inv, extra, as_date_cell=(k % 2 == 0))
        doc_id = f"xlsx_{inv['invoice_number']}"
        path, rel = place("spreadsheet", doc_id + ".xlsx"); wb.save(path)
        docs.append(make_doc(doc_id, "northwind", "spreadsheet", rel, "xlsx", [sheet_gt(inv)], labeled,
                             notes="key-value header block + item table" + ("; date stored as Excel date" if k % 2 == 0 else "")))
    if nw_multi:
        wb = Workbook(); wb.remove(wb.active)
        invs = []
        for p in nw_multi:
            inv, labeled, extra = nw[p]
            write_invoice_sheet(wb.create_sheet(f"INV-{inv['invoice_number']}"), inv, extra, as_date_cell=True)
            invs.append(sheet_gt(inv))
        notes_ws = wb.create_sheet("Notes")
        notes_ws.append(["This sheet contains no invoice and must be ignored."])
        path, rel = place("spreadsheet", "xlsx_multisheet.xlsx"); wb.save(path)
        docs.append(make_doc("xlsx_multisheet", "northwind", "spreadsheet", rel, "xlsx", invs, labeled,
                             notes=f"{len(invs)} invoices on separate sheets + 1 non-invoice sheet"))
        if shutil.which("soffice"):
            subprocess.run(["soffice", "--headless", "--convert-to", "xls", "--outdir", str(path.parent), str(path)],
                           capture_output=True, timeout=120)
            xls = path.with_suffix(".xls")
            if xls.exists():
                docs.append(make_doc("xls_multisheet", "northwind", "spreadsheet", str(xls.relative_to(out)), "xls",
                                     invs, labeled, notes="legacy .xls converted with LibreOffice"))
    csv_items = sheet_items[n_xlsx:]
    # prefer a customer with non-ASCII characters for the cp1252 file
    csv_items.sort(key=lambda p: nw[p][0]["customer_name"].isascii())
    for k, p in enumerate(csv_items):
        inv, labeled, _ = nw[p]
        if k == 0:
            variant, enc, delim, dec = "cp1252", "cp1252", ",", False
        elif k == 1:
            variant, enc, delim, dec = "semicolon_decimal_comma", "utf-8", ";", True
        else:
            variant, enc, delim, dec = "standard", "utf-8", ",", False
        doc_id = f"csv_{inv['invoice_number']}"
        path, rel = place("spreadsheet", doc_id + ".csv")
        with open(path, "w", newline="", encoding=enc, errors="replace") as f:
            w = csv.writer(f, delimiter=delim); w.writerow(CSV_HEADER); w.writerows(flat_rows(inv, dec))
        docs.append(make_doc(doc_id, "northwind", "spreadsheet", rel, "csv", [sheet_gt(inv)], labeled,
                             notes=f"one row per line item; variant={variant}; encoding={enc}; delimiter='{delim}'"))

    # ---------------- Ledger for PDF <-> Excel cross-verification (covers the eval PDFs)
    if nw_eval:
        ledger_invs = [sheet_gt(nw[p][0]) for p in nw_eval]
        mismatches = []
        for led in ledger_invs[:3]:
            it = led["line_items"][0]
            old_total = led["total"]
            it["quantity"] += 1
            it["amount"] = round(it["quantity"] * it["unit_price"], 2)
            led["total"] = round(sum(x["amount"] for x in led["line_items"]), 2)
            mismatches.append({"invoice_number": led["invoice_number"], "field": "total",
                               "pdf_value": old_total, "ledger_value": led["total"]})
            mismatches.append({"invoice_number": led["invoice_number"], "field": "line_items[0].quantity",
                               "pdf_value": it["quantity"] - 1, "ledger_value": it["quantity"]})
        removed = ledger_invs.pop()  # one PDF invoice is missing from the ledger
        mismatches.append({"invoice_number": removed["invoice_number"], "field": "missing_in_ledger",
                           "pdf_value": removed["total"], "ledger_value": None})
        wb = Workbook(); ws = wb.active; ws.title = "Ledger"
        ws.append(CSV_HEADER)
        for led in ledger_invs:
            for r in flat_rows(led):
                ws.append([r[0], r[1], r[2], r[3], float(r[4]), r[5], float(r[6]), float(r[7])])
        path, rel = place("cross_verify", "northwind_ledger.xlsx"); wb.save(path)
        docs.append(make_doc("northwind_ledger", "northwind", "cross_verify", rel, "xlsx", ledger_invs,
                             ["invoice_number", "invoice_date", "customer_name", "total", "line_items"],
                             notes="ledger to cross-check against the northwind_pdf documents",
                             cross_verify_against=[f"northwind_{nw[p][0]['invoice_number']}" for p in nw_eval],
                             expected_mismatches=mismatches))

    # ---------------- Planted errors (digital PDFs edited in place)
    for k, p in enumerate(nw_plant):
        inv, labeled, _ = nw[p]
        inv = json.loads(json.dumps(inv))
        doc_id = f"planted_{inv['invoice_number']}"
        dst, rel = place("planted_error", doc_id + ".pdf")
        try:
            if k % 2 == 0:  # wrong grand total
                new_total = round(inv["total"] * 1.10, 2)
                replace_text_on_pdf(p, dst, f"${inv['total']:.2f}", f"${new_total:.2f}", row_hint="Total:")
                flags = ["total_mismatch"]
            else:           # wrong line amount
                it = inv["line_items"][0]
                new_amt = round(it["amount"] + 10, 2)
                replace_text_on_pdf(p, dst, f"${it['amount']:.2f}", f"${new_amt:.2f}", row_hint=it["description"])
                flags = ["line_item_math", "total_mismatch"]
            parsed, _, _, _ = northwind_parse(dst)  # ground truth = what is now printed
        except Exception as e:  # noqa: BLE001
            dst.unlink(missing_ok=True); excluded[f"planted: {type(e).__name__}"] += 1; continue
        docs.append(make_doc(doc_id, "northwind", "planted_error", rel, "pdf", [parsed], labeled,
                             flags=flags, notes="printed values edited so the math no longer adds up"))

    # ---------------- Duplicates
    for p in nw_eval[:2]:
        inv, labeled, _ = nw[p]
        doc_id = f"dup_exact_{inv['invoice_number']}"
        _, rel = place("duplicate", doc_id + ".pdf", src=p)
        docs.append(make_doc(doc_id, "northwind", "duplicate", rel, "pdf", [inv], labeled, flags=["duplicate"],
                             duplicate_of=f"northwind_{inv['invoice_number']}", notes="byte-identical copy, new file name"))
    if len(nw_eval) > 2:
        inv, labeled, _ = nw[nw_eval[2]]
        pix = pymupdf.open(nw_eval[2])[0].get_pixmap(dpi=200)
        doc_id = f"dup_png_{inv['invoice_number']}"
        path, rel = place("duplicate", doc_id + ".png"); pix.save(path)
        docs.append(make_doc(doc_id, "northwind", "duplicate", rel, "png", [inv], labeled, flags=["duplicate"],
                             duplicate_of=f"northwind_{inv['invoice_number']}", notes="same invoice rendered as PNG (different bytes)"))
    kat = [d for d in docs if d["source"] == "katana" and "invoice_number" in d["labeled_fields"]]
    if kat:
        src = kat[0]
        doc_id = f"dup_jpg_{src['doc_id']}"
        path, rel = place("duplicate", doc_id + ".jpg")
        Image.open(out / src["file"]).convert("RGB").save(path, "JPEG", quality=85)
        docs.append(make_doc(doc_id, "katana", "duplicate", rel, "jpg", src["invoices"], src["labeled_fields"],
                             flags=["duplicate"], duplicate_of=src["doc_id"], notes="same invoice re-saved as JPEG"))

    # ---------------- Corrupted / edge cases
    sample_pdf = nw_eval[0] if nw_eval else (nw_paths[0] if nw_paths else None)
    if sample_pdf:
        b = Path(sample_pdf).read_bytes()
        _, rel = place("corrupted", "truncated.pdf", data=b[: int(len(b) * 0.4)])
        docs.append(make_doc("corrupt_truncated_pdf", "generated", "corrupted", rel, "pdf", [], [], outcome="fail",
                             notes="first 40% of a valid PDF"))
        w = PdfWriter(); [w.add_page(pg) for pg in PdfReader(str(sample_pdf)).pages]; w.encrypt("secret123")
        path, rel = place("corrupted", "password_protected.pdf")
        with open(path, "wb") as f: w.write(f)
        docs.append(make_doc("corrupt_password_pdf", "generated", "corrupted", rel, "pdf", [], [], outcome="fail",
                             notes="encrypted with a user password; should fail with a clear 'encrypted' error"))
    _, rel = place("corrupted", "empty.pdf", data=b"")
    docs.append(make_doc("corrupt_empty_pdf", "generated", "corrupted", rel, "pdf", [], [], outcome="fail", notes="0 bytes"))
    _, rel = place("corrupted", "random_bytes.xlsx", data=rng.randbytes(4096))
    docs.append(make_doc("corrupt_random_xlsx", "generated", "corrupted", rel, "xlsx", [], [], outcome="fail", notes="random bytes"))
    _, rel = place("corrupted", "random_bytes.tiff", data=rng.randbytes(4096))
    docs.append(make_doc("corrupt_random_tiff", "generated", "corrupted", rel, "tiff", [], [], outcome="fail", notes="random bytes"))
    path, rel = place("corrupted", "header_only.csv")
    path.write_text(",".join(CSV_HEADER) + "\n", encoding="utf-8")
    docs.append(make_doc("edge_header_only_csv", "generated", "corrupted", rel, "csv", [], [], outcome="no_invoice",
                         notes="valid CSV with no data rows: should finish with 0 invoices, not crash"))
    if kat:  # image with the wrong extension: content sniffing should still process it
        src = kat[-1]
        path, rel = place("corrupted", "image_named_as.pdf")
        Image.open(out / src["file"]).convert("RGB").save(path, "JPEG", quality=90)
        docs.append(make_doc("edge_jpg_named_pdf", "katana", "corrupted", rel, "pdf", src["invoices"], src["labeled_fields"],
                             outcome="success", notes="JPEG bytes with a .pdf extension; detect type by content"))
        # multi-page TIFF made of two invoices
        a_img = Image.open(out / kat[0]["file"]).convert("L")
        b_img = Image.open(out / kat[min(1, len(kat) - 1)]["file"]).convert("L")
        path, rel = place("image_ocr", "multipage.tiff")
        a_img.save(path, save_all=True, append_images=[b_img], compression="tiff_deflate")
        docs.append(make_doc("tiff_multipage", "katana", "image_ocr", rel, "tiff",
                             kat[0]["invoices"] + kat[min(1, len(kat) - 1)]["invoices"],
                             sorted(set(kat[0]["labeled_fields"]) & set(kat[min(1, len(kat) - 1)]["labeled_fields"]),
                                    key=FIELDS.index),
                             notes="2-frame TIFF, one invoice per frame"))

    # ---------------- SROIE
    samples, sroie_fmt = find_sroie_samples(sroie_dir)
    has_labels = sroie_dir.exists() and any(q.name.lower() in {"samples.json", "sample.json"} and q.stat().st_size > 10000
                                            for q in sroie_dir.rglob("*.json"))
    if not samples and not has_labels and sroie_dir.exists() and not a.no_download and \
            any(q.suffix.lower() in IMG_EXT for q in sroie_dir.rglob("*") if q.is_file()):
        print("sroie: images found but no labels; downloading samples.json (labels, ~23 MB) from Hugging Face...")
        try:
            tmp = sroie_dir / "samples.json.download"
            urllib.request.urlretrieve(SROIE_LABELS_URL, tmp)
            json.load(open(tmp, encoding="utf-8"))  # make sure it is real JSON
            tmp.rename(sroie_dir / "samples.json")
            samples, sroie_fmt = find_sroie_samples(sroie_dir)
        except Exception as e:  # noqa: BLE001
            print(f"  WARNING: could not download SROIE labels ({type(e).__name__}: {e})")
    labels_file = next((q for q in sroie_dir.rglob("*.json") if q.name.lower() in {"samples.json", "sample.json"}), None) \
        if sroie_dir.exists() else None
    if len(samples) < a.n_sroie and labels_file is not None and not a.no_download:
        # Local images do not carry the original SROIE names -> fetch the receipts we need by their real names.
        try:
            all_s = [x for x in json.load(open(labels_file, encoding="utf-8")).get("samples", []) if x.get("filepath")]
        except Exception:  # noqa: BLE001
            all_s = []
        if all_s:
            pick = random.Random(a.seed).sample(all_s, min(a.n_sroie, len(all_s)))
            print(f"sroie: local image names do not match the labels; downloading {len(pick)} receipts by their SROIE ids...")
            got = 0
            for x in pick:
                dst = labels_file.parent / x["filepath"]
                if dst.is_file():
                    got += 1; continue
                dst.parent.mkdir(parents=True, exist_ok=True)
                try:
                    urllib.request.urlretrieve(SROIE_IMG_URL + x["filepath"], dst.with_suffix(".download"))
                    Image.open(dst.with_suffix(".download")).verify()
                    dst.with_suffix(".download").rename(dst); got += 1
                except Exception as e:  # noqa: BLE001
                    dst.with_suffix(".download").unlink(missing_ok=True)
                    print(f"  could not download {x['filepath']} ({type(e).__name__})")
            print(f"  {got}/{len(pick)} receipts downloaded into {(labels_file.parent / 'data').relative_to(R) if (labels_file.parent / 'data').is_relative_to(R) else labels_file.parent / 'data'}")
            samples, sroie_fmt = find_sroie_samples(sroie_dir)
    if samples:
        print(f"sroie: {len(samples)} labeled images found ({sroie_fmt})")
        rng.shuffle(samples)
        for s in samples[: a.n_sroie]:
            inv, labeled = empty_invoice(), []
            if s.get("company"):
                inv["supplier_name"] = s["company"].strip(); labeled.append("supplier_name")
            d = parse_date(s.get("date"), SROIE_DATE_FORMATS)
            if d:
                inv["invoice_date"] = d; labeled.append("invoice_date")
            else:
                excluded["sroie: date not parseable (unlabeled)"] += 1
            t = parse_amount(s.get("total"))
            if t is not None:
                inv["total"] = t; labeled.append("total")
            stem = Path(s["image"]).stem
            doc_id = f"sroie_{stem}"
            ext = Path(s["image"]).suffix.lower() or ".jpg"
            _, rel = place("sroie", doc_id + ext, src=s["image"])
            docs.append(make_doc(doc_id, "sroie", "real_scan", rel, ext.lstrip("."), [inv], labeled,
                                 notes=f"receipt; raw date='{s.get('date')}', raw total='{s.get('total')}'",
                                 address=s.get("address")))
        print(f"sroie: {sum(d['source'] == 'sroie' for d in docs)} docs")
    else:
        print(f"sroie: no labeled receipts found under {sroie_dir}, skipped")
        if sroie_dir.exists():
            exts = Counter(q.suffix.lower() or "(none)" for q in sroie_dir.rglob("*") if q.is_file())
            print(f"  files in that folder by type: {dict(exts.most_common(8))}")
            ex = next((q.name for q in sroie_dir.rglob("*") if q.suffix.lower() in IMG_EXT), None)
            if ex and any((sroie_dir / n).exists() for n in ["samples.json", "sample.json"]):
                print(f"  labels were downloaded, but image names (e.g. '{ex}') do not match the SROIE ids "
                      "(e.g. 'X51005442376.jpg'). Download the images from the same Hugging Face dataset.")
            print("  Supported: Voxel51 samples.json, ICDAR/Kaggle per-image label files ({\"company\":..., \"total\":...}),")
            print("  a CSV with company/date/total columns, or Parquet with those columns.")

    # ---------------- Long merged PDF (speed + invoice splitting)
    if not a.no_long_pdf and nw:
        merged = pymupdf.open()
        invs = []
        for p in sorted(nw):
            merged.insert_pdf(pymupdf.open(p)); invs.append(nw[p][0])
        path, rel = place("long_pdf", "northwind_merged.pdf"); merged.save(path, garbage=3, deflate=True)
        docs.append(make_doc("long_northwind_merged", "northwind", "long_pdf", rel, "pdf", invs,
                             ["invoice_number", "invoice_date", "customer_name", "total", "line_items"],
                             notes=f"{merged.page_count} pages, one invoice per page; score invoice count, numbers, totals and time",
                             pages=merged.page_count))

    # ---------------- write
    for d in docs:
        write_gt(out, d)
    with open(out / "manifest.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["doc_id", "source", "category", "file", "file_type", "expected_outcome",
                    "expected_flags", "n_invoices", "labeled_fields"])
        for d in docs:
            w.writerow([d["doc_id"], d["source"], d["category"], d["file"], d["file_type"], d["expected_outcome"],
                        "|".join(d["expected_flags"]), len(d["invoices"]), "|".join(d["labeled_fields"])])

    by_cat = Counter(d["category"] for d in docs)
    card = ["# Evaluation dataset card", "",
            f"Generated by `prepare_dataset.py` (seed {a.seed}). {len(docs)} documents.", "",
            "| Category | Documents |", "|---|---|"] + [f"| {k} | {v} |" for k, v in sorted(by_cat.items())] + [
            "", "## Sources", "",
            "- Katana ML invoices-donut-data-v1 (MIT): https://huggingface.co/datasets/katanaml-org/invoices-donut-data-v1",
            "- Northwind invoices (Apache-2.0): https://huggingface.co/datasets/AyoubChLin/northwind_invocies",
            "- SROIE via Voxel51 (CC-BY-4.0): https://huggingface.co/datasets/Voxel51/scanned_receipts",
            "- Mendeley \"Samples of electronic invoices\" (CC BY 4.0), Kozlowski & Weichbroth 2021, doi:10.17632/tnj49gpmtz.2 "
            "- labels taken from the matching Katana row (matched by invoice number and total).",
            "- Generated: spreadsheets, degraded scans, planted errors, duplicates, corrupted files, long PDF "
            "(all derived from the Northwind/Katana documents above).", "",
            "## Label rules", "",
            "- Only fields in `labeled_fields` are scored. A field that is labeled and null means the correct output is null.",
            "- Northwind invoices do not print supplier, subtotal or tax, so those are labeled null.",
            "- Katana `supplier_name`/`customer_name` include the address; score them with a partial/fuzzy match.",
            "- Katana rows whose line items do not add up to the subtotal (flattened labels) have `line_items` unlabeled.",
            "- SROIE labels only supplier (company), date and total; currency is not labeled.", "",
            "## Exclusions and partial labels", ""] + \
           ([f"- {k}: {v}" for k, v in excluded.items()] or ["- none"])
    (out / "DATASET_CARD.md").write_text("\n".join(card) + "\n", encoding="utf-8")

    print("\nDocuments by category:")
    for k, v in sorted(by_cat.items()):
        print(f"  {k:16s} {v}")
    print(f"  {'TOTAL':16s} {len(docs)}")
    if excluded:
        print("\nExcluded / partially labeled:")
        for k, v in excluded.items():
            print(f"  {k}: {v}")
    print(f"\nWrote {out}/manifest.csv, {out}/gt/*.json, {out}/DATASET_CARD.md")


if __name__ == "__main__":
    main()
