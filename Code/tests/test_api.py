"""API tests: upload -> background processing -> results, corrections, reprocessing, search, duplicates,
cross-verification, auth and limits. Uses a temporary SQLite database and runs jobs synchronously.
Run:  pytest Code/tests -v
"""
import csv
import io
import shutil
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_ingest import make_digital_pdf, make_invoice_image  # noqa: E402

from api import config, db, processing  # noqa: E402


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("QUEUE_MODE", "sync")
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "uploads"))
    monkeypatch.setenv("API_KEY", "")
    monkeypatch.setenv("EMBED_PROVIDER", "hash")
    monkeypatch.setenv("DOCINTEL_LLM_PROVIDER", "none")
    monkeypatch.setenv("DOCINTEL_OCR", "tesseract")
    config.reload_settings()
    db.reset_engine()
    processing._llm = None
    from api.main import app
    with TestClient(app) as c:
        yield c
    db.reset_engine()


def upload(client, *paths, headers=None):
    files = [("files", (Path(p).name, open(p, "rb"))) for p in paths]
    r = client.post("/api/documents", files=files, headers=headers or {})
    for _, (_, fh) in files:
        fh.close()
    return r


def results(client, doc_id):
    return client.get(f"/api/documents/{doc_id}/results").json()


def ledger_csv(path, total="840.00", qty="40"):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["invoice_number", "invoice_date", "customer_name", "product", "unit_price", "quantity", "line_total", "invoice_total"])
        w.writerow(["10253", "2016-07-10", "Hanari Carnes", "Gorgonzola Telino", "10.00", "20", "200.00", total])
        w.writerow(["10253", "2016-07-10", "Hanari Carnes", "Maxilaku", "16.00", qty, f"{16 * int(qty):.2f}", total])
    return path


# ---------------------------------------------------------------- basics
def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok" and r.json()["queue"]["mode"] == "sync"


def test_upload_digital_pdf_end_to_end(client, tmp_path):
    r = upload(client, make_digital_pdf(tmp_path / "inv.pdf"))
    assert r.status_code == 201
    doc = r.json()["documents"][0]
    d = client.get(f"/api/documents/{doc['id']}").json()
    assert d["status"] in ("valid", "needs_review") and d["detected_type"] == "pdf" and d["page_count"] == 1
    assert [e["stage"] for e in d["events"]][-1] == "done"
    inv = results(client, doc["id"])["invoices"][0]
    assert inv["fields"]["invoice_number"] == "10253" and inv["fields"]["total"] == 840.0
    assert inv["fields"]["supplier_name"] is None                       # not printed -> not invented
    assert any(i["rule"] == "MISSING_FIELD" and i["field"] == "supplier_name" for i in inv["issues"])
    assert len(inv["line_items"]) == 2
    assert client.get(f"/api/documents/{doc['id']}/file").status_code == 200


def test_multi_file_upload_and_listing(client, tmp_path):
    p1 = make_digital_pdf(tmp_path / "a.pdf")
    p2 = ledger_csv(tmp_path / "b.csv")
    r = upload(client, p1, p2)
    assert r.status_code == 201 and len(r.json()["documents"]) == 2
    lst = client.get("/api/documents").json()
    assert lst["total"] == 2
    csv_doc = [d for d in lst["items"] if d["filename"] == "b.csv"][0]
    inv = results(client, csv_doc["id"])["invoices"][0]
    assert inv["method"] == "table_mapping" and inv["fields"]["total"] == 840.0


def test_xlsx_multi_sheet(client, tmp_path):
    wb = Workbook(); ws = wb.active; ws.title = "A"
    for row in [["Invoice Number", 1], ["Invoice Date", "2020-01-05"], ["Customer Name", "X Ltd"],
                ["Product Name", "Unit Price", "Quantity", "Total Price"], ["Pen", 2, 5, 10], [None, None, "Total:", 10]]:
        ws.append(row)
    ws2 = wb.create_sheet("B")
    for row in [["Invoice Number", 2], ["Invoice Date", "2020-01-06"], ["Customer Name", "Y Ltd"], [None, None, "Total:", 5]]:
        ws2.append(row)
    wb.save(tmp_path / "w.xlsx")
    doc = upload(client, tmp_path / "w.xlsx").json()["documents"][0]
    invs = results(client, doc["id"])["invoices"]
    assert [i["fields"]["invoice_number"] for i in invs] == ["1", "2"]
    assert not any(x["rule"] == "DUPLICATE" for i in invs for x in i["issues"])   # same file, different invoices


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
def test_scanned_image_upload(client, tmp_path):
    p = tmp_path / "scan.png"; make_invoice_image(p)
    doc = upload(client, p).json()["documents"][0]
    d = client.get(f"/api/documents/{doc['id']}").json()
    assert d["ocr_pages"] == 1 and d["status"] != "failed"
    r = results(client, doc["id"], )
    assert r["invoices"][0]["fields"]["invoice_number"] == "10253"


# ---------------------------------------------------------------- failures and reprocessing
def test_corrupted_file_fails_cleanly_and_can_be_reprocessed(client, tmp_path):
    good = make_digital_pdf(tmp_path / "g.pdf").read_bytes()
    bad = tmp_path / "broken.pdf"; bad.write_bytes(good[: len(good) // 3])
    doc = upload(client, bad).json()["documents"][0]
    d = client.get(f"/api/documents/{doc['id']}").json()
    assert d["status"] == "failed" and d["error_code"] == "CORRUPT_FILE"
    assert "uploads" not in d["error_message"] and str(tmp_path) not in d["error_message"]   # no server paths leak
    r = client.post(f"/api/documents/{doc['id']}/reprocess")
    assert r.status_code == 200
    d = client.get(f"/api/documents/{doc['id']}").json()
    assert d["status"] == "failed" and d["attempts"] == 2


def test_reprocess_replaces_results(client, tmp_path):
    doc = upload(client, make_digital_pdf(tmp_path / "a.pdf")).json()["documents"][0]
    first = results(client, doc["id"])["invoices"]
    client.post(f"/api/documents/{doc['id']}/reprocess")
    second = results(client, doc["id"])["invoices"]
    assert len(first) == len(second) == 1 and second[0]["fields"] == first[0]["fields"]


def test_unsupported_and_too_large_files_rejected(client, tmp_path, monkeypatch):
    exe = tmp_path / "virus.exe"; exe.write_bytes(b"MZ" + b"0" * 100)
    r = upload(client, exe)
    assert r.status_code == 415
    monkeypatch.setattr(config.settings, "max_upload_mb", 0.0001)
    r = upload(client, make_digital_pdf(tmp_path / "big.pdf"))
    assert r.status_code == 400 and r.json()["detail"]["rejected"][0]["status_code"] == 413


def test_api_key_required_when_configured(client, tmp_path, monkeypatch):
    monkeypatch.setattr(config.settings, "api_key", "secret")
    assert client.get("/api/documents").status_code == 401
    assert client.get("/api/documents", headers={"X-API-Key": "secret"}).status_code == 200
    assert client.get("/api/health").status_code == 200    # health stays open for monitoring


# ---------------------------------------------------------------- duplicates against the database
def test_duplicate_detected_across_uploads(client, tmp_path):
    p = make_digital_pdf(tmp_path / "a.pdf")
    d1 = upload(client, p).json()["documents"][0]
    shutil.copy(p, tmp_path / "copy_of_a.pdf")
    d2 = upload(client, tmp_path / "copy_of_a.pdf").json()["documents"][0]
    assert not any(x["rule"] == "DUPLICATE" for x in results(client, d1["id"])["invoices"][0]["issues"])
    dup = [x for x in results(client, d2["id"])["invoices"][0]["issues"] if x["rule"] == "DUPLICATE"]
    assert dup and dup[0]["details"]["duplicate_of_document"] == d1["id"]
    assert client.get(f"/api/documents/{d2['id']}").json()["status"] == "needs_review"
    # correcting the original later must not turn it into a "duplicate" of the newer copy
    inv1 = results(client, d1["id"])["invoices"][0]
    r = client.patch(f"/api/invoices/{inv1['id']}", json={"fields": {"supplier_name": "Northwind Traders"}}).json()
    assert not any(x["rule"] == "DUPLICATE" for x in r["issues"])


# ---------------------------------------------------------------- corrections
def test_correction_revalidates_and_is_audited(client, tmp_path):
    doc = upload(client, ledger_csv(tmp_path / "l.csv")).json()["documents"][0]
    inv = results(client, doc["id"])["invoices"][0]
    r = client.patch(f"/api/invoices/{inv['id']}", json={"fields": {"total": "999.00"}, "corrected_by": "tester"})
    assert r.status_code == 200
    body = r.json()
    assert body["fields"]["total"] == 999.0 and body["field_meta"]["total"]["status"] == "corrected"
    assert any(x["rule"] == "LINE_SUM_MISMATCH" for x in body["issues"])
    r = client.patch(f"/api/invoices/{inv['id']}", json={"fields": {"total": "$840.00", "supplier_name": "Northwind Traders",
                                                                     "currency": "usd", "invoice_date": "10/07/2016"}})
    body = r.json()
    assert body["fields"]["total"] == 840.0 and body["fields"]["currency"] == "USD"
    assert not any(x["rule"] in ("LINE_SUM_MISMATCH", "MISSING_FIELD") for x in body["issues"])
    # 10/07/2016 is read as 2016-07-10 = the stored date, so it is not recorded as a change
    assert [c["field"] for c in body["corrections"]] == ["total", "total", "supplier_name", "currency"]
    assert client.patch(f"/api/invoices/{inv['id']}", json={"fields": {"total": "abc"}}).status_code == 422
    assert client.patch(f"/api/invoices/{inv['id']}", json={"fields": {"sha256": "x"}}).status_code == 422


def test_correct_line_items(client, tmp_path):
    doc = upload(client, ledger_csv(tmp_path / "l.csv")).json()["documents"][0]
    inv = results(client, doc["id"])["invoices"][0]
    r = client.patch(f"/api/invoices/{inv['id']}", json={"line_items": [
        {"description": "Gorgonzola Telino", "quantity": 20, "unit_price": 10, "amount": 250}]})
    rules = {x["rule"] for x in r.json()["issues"]}
    assert {"LINE_ITEM_MATH", "LINE_SUM_MISMATCH"} <= rules


# ---------------------------------------------------------------- search
def test_search_keyword_semantic_hybrid_and_filters(client, tmp_path):
    upload(client, make_digital_pdf(tmp_path / "a.pdf"))
    upload(client, ledger_csv(tmp_path / "b.csv", total="120.00", qty="5"))
    r = client.get("/api/search", params={"q": "Hanari", "mode": "keyword"}).json()
    assert r["count"] >= 1 and r["results"][0]["customer_name"] == "Hanari Carnes"
    r = client.get("/api/search", params={"q": "Hanary Karnes gorgonsola", "mode": "semantic"}).json()  # misspelled
    assert r["count"] >= 1 and "Hanari" in (r["results"][0]["customer_name"] or "")
    r = client.get("/api/search", params={"q": "Maxilaku", "mode": "hybrid", "min_total": 500}).json()
    assert r["count"] == 1 and r["results"][0]["total"] == 840.0
    r = client.get("/api/search", params={"min_total": 100, "max_total": 200}).json()
    assert r["mode"] == "filter" and [x["total"] for x in r["results"]] == [120.0]
    r = client.get("/api/search", params={"q": "nothing-like-this-zzz", "mode": "keyword"}).json()
    assert r["count"] == 0


# ---------------------------------------------------------------- cross-verification
def test_cross_verify_pdf_vs_ledger(client, tmp_path):
    pdf = upload(client, make_digital_pdf(tmp_path / "inv.pdf")).json()["documents"][0]
    led = upload(client, ledger_csv(tmp_path / "ledger.csv", total="856.00", qty="41")).json()["documents"][0]
    r = client.post("/api/cross-verify", json={"left_document_ids": [pdf["id"]], "right_document_ids": [led["id"]],
                                               "left_name": "pdf", "right_name": "ledger"}).json()
    fields = {m["field"] for m in r["mismatches"]}
    assert r["matched"] == 1 and "total" in fields and any(f.endswith("quantity") for f in fields)


def test_stats(client, tmp_path):
    upload(client, make_digital_pdf(tmp_path / "a.pdf"))
    st = client.get("/api/stats").json()
    assert st["documents"] == 1 and st["invoices"] == 1


# ---------------------------------------------------------------- embeddings through an OpenAI-compatible endpoint (e.g. Ollama)
def test_openai_compatible_embeddings(client, tmp_path, monkeypatch):
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from api import embeddings

    class H(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = _json.loads(self.rfile.read(int(self.headers["content-length"])))
            data = [{"index": i, "embedding": embeddings._hash_embed(t)} for i, t in enumerate(body["input"])]
            out = _json.dumps({"data": data}).encode()
            self.send_response(200); self.send_header("content-type", "application/json"); self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(config.settings, "embed_provider", "openai")
    monkeypatch.setattr(config.settings, "embed_model", "fake-embed")
    monkeypatch.setattr(config.settings, "embed_base_url", f"http://127.0.0.1:{srv.server_port}/v1")
    upload(client, make_digital_pdf(tmp_path / "a.pdf"))
    r = client.get("/api/search", params={"q": "Hanary Karnes", "mode": "semantic"}).json()
    srv.shutdown()
    assert r["count"] >= 1 and r["results"][0]["customer_name"] == "Hanari Carnes"


def test_ledger_rows_are_not_duplicates_of_the_invoices(client, tmp_path):
    led = tmp_path / "ledger.csv"
    with open(led, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["invoice_number", "invoice_date", "customer_name", "product", "unit_price", "quantity", "line_total", "invoice_total"])
        w.writerow(["10253", "2016-07-10", "Hanari Carnes", "Gorgonzola Telino", "10.00", "20", "200.00", "840.00"])
        w.writerow(["10253", "2016-07-10", "Hanari Carnes", "Maxilaku", "16.00", "40", "640.00", "840.00"])
        w.writerow(["10254", "2016-07-11", "Chop-suey Chinese", "Tofu", "8.00", "21", "168.00", "168.00"])
    upload(client, led)                                         # ledger first
    pdf = upload(client, make_digital_pdf(tmp_path / "inv.pdf")).json()["documents"][0]
    assert not any(x["rule"] == "DUPLICATE" for x in results(client, pdf["id"])["invoices"][0]["issues"])


def test_page_preview(client, tmp_path):
    d = upload(client, make_digital_pdf(tmp_path / "a.pdf", pages=2)).json()["documents"][0]
    r = client.get(f"/api/documents/{d['id']}/preview", params={"page": 2})
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"
    assert client.get(f"/api/documents/{d['id']}/preview", params={"page": 3}).status_code == 404
    c = upload(client, ledger_csv(tmp_path / "l.csv")).json()["documents"][0]
    assert client.get(f"/api/documents/{c['id']}/preview").status_code == 404
