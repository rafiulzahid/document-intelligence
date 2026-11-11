#!/usr/bin/env python3
"""
run_ingest.py - run ingestion over the evaluation set and report what happened.

    python Code/run_ingest.py                       # all documents in data/eval/manifest.csv
    python Code/run_ingest.py --category real_scan  # one category
    python Code/run_ingest.py --file some.pdf       # a single file, prints its text
    python Code/run_ingest.py --ocr tesseract       # force an OCR backend (auto | tesseract | easyocr)

Writes data/ingest_out/results.jsonl (one line per document), data/ingest_out/text/<doc_id>.txt
and data/ingest_out/summary.md. These timings and success/failure counts feed the evaluation report.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from docintel.ingest import ingest_file  # noqa: E402
from docintel.ingest.ocr import get_backend  # noqa: E402


def pct(vals, q):
    if not vals:
        return 0.0
    vals = sorted(vals)
    k = max(0, min(len(vals) - 1, round(q / 100 * (len(vals) - 1))))
    return vals[k]


def outcome_ok(expected: str, r) -> bool:
    if expected == "fail":
        return not r.ok
    if expected == "no_invoice":
        return r.ok and any("NO_DATA_ROWS" in w for w in r.warnings)
    return r.ok and (any(p.text.strip() for p in r.pages) or any(s.n_rows > 1 for s in r.sheets))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path, default=ROOT / "data" / "eval" / "manifest.csv")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "ingest_out")
    ap.add_argument("--ocr", default="auto", help="auto | tesseract | easyocr")
    ap.add_argument("--category", help="only this category (e.g. real_scan, digital_pdf, spreadsheet)")
    ap.add_argument("--skip-long", action="store_true", help="skip the long merged PDF")
    ap.add_argument("--limit", type=int, help="process at most N documents")
    ap.add_argument("--file", type=Path, help="ingest one file and print its text")
    a = ap.parse_args()

    if a.file:
        r = ingest_file(a.file, ocr=a.ocr)
        print(json.dumps(r.to_dict(include_text=False), indent=2, ensure_ascii=False))
        print("\n" + "=" * 80 + "\n" + r.full_text()[:6000])
        return

    base = a.manifest.parent
    rows = list(csv.DictReader(open(a.manifest, encoding="utf-8")))
    if a.category:
        rows = [r for r in rows if r["category"] == a.category]
    if a.skip_long:
        rows = [r for r in rows if r["category"] != "long_pdf"]
    if a.limit:
        rows = rows[: a.limit]
    (a.out / "text").mkdir(parents=True, exist_ok=True)

    try:
        b = get_backend(a.ocr)
        print(f"OCR backend: {b.name}" + (f" (GPU={getattr(b, 'gpu', False)})" if b.name == "easyocr" else ""))
    except Exception as e:  # noqa: BLE001
        print(f"WARNING: no OCR backend available ({e}); scanned pages will fail")

    results, t_start = [], time.perf_counter()
    with open(a.out / "results.jsonl", "w", encoding="utf-8") as fout:
        for k, row in enumerate(rows, 1):
            path = base / row["file"]
            last = [0.0]

            def progress(done, total, _id=row["doc_id"]):
                if total >= 20 and time.perf_counter() - last[0] > 2:
                    last[0] = time.perf_counter()
                    print(f"    {_id}: page {done}/{total}", flush=True)

            r = ingest_file(path, ocr=a.ocr, progress=progress)
            good = outcome_ok(row["expected_outcome"], r)
            confs = [p.ocr_confidence for p in r.pages if p.ocr_confidence is not None]
            rec = {"doc_id": row["doc_id"], "category": row["category"], "file_type": row["file_type"],
                   "expected_outcome": row["expected_outcome"], "status": r.status, "outcome_as_expected": good,
                   "error_code": r.error_code, "error_message": r.error_message, "detected_type": r.detected_type,
                   "pages": r.page_count, "ocr_pages": r.ocr_pages, "sheets": len(r.sheets),
                   "chars": len(r.full_text()), "mean_ocr_conf": round(sum(confs) / len(confs), 3) if confs else None,
                   "time_ms": r.timings_ms.get("total"), "warnings": r.warnings,
                   "page_warnings": sorted({w for p in r.pages for w in p.warnings if not w.startswith("preprocess")})}
            fout.write(json.dumps(rec, ensure_ascii=False) + "\n")
            (a.out / "text" / f"{row['doc_id']}.txt").write_text(r.full_text(), encoding="utf-8")
            results.append(rec)
            mark = "ok " if good else "!! "
            print(f"[{k:3d}/{len(rows)}] {mark}{row['doc_id']:38s} {r.status:6s} "
                  f"{(r.error_code or ''):14s} pages={r.page_count:<4d} ocr={r.ocr_pages:<4d} "
                  f"{r.timings_ms.get('total', 0) / 1000:7.2f}s", flush=True)

    # ---------------- summary
    by = defaultdict(list)
    for r in results:
        by[r["category"]].append(r)
    lines = ["# Ingestion summary", "", f"Documents: {len(results)} · wall time {time.perf_counter() - t_start:.1f}s", "",
             "| Category | Docs | As expected | Failed (unexpected) | Pages | OCR pages | Mean OCR conf | p50 s/doc | p95 s/doc |",
             "|---|---|---|---|---|---|---|---|---|"]
    for cat, rs in sorted(by.items()):
        t = [x["time_ms"] / 1000 for x in rs]
        cf = [x["mean_ocr_conf"] for x in rs if x["mean_ocr_conf"] is not None]
        unexpected = sum(1 for x in rs if x["status"] == "failed" and x["expected_outcome"] != "fail")
        lines.append(f"| {cat} | {len(rs)} | {sum(x['outcome_as_expected'] for x in rs)} | {unexpected} | "
                     f"{sum(x['pages'] for x in rs)} | {sum(x['ocr_pages'] for x in rs)} | "
                     f"{statistics.mean(cf):.2f} | {pct(t, 50):.2f} | {pct(t, 95):.2f} |" if cf else
                     f"| {cat} | {len(rs)} | {sum(x['outcome_as_expected'] for x in rs)} | {unexpected} | "
                     f"{sum(x['pages'] for x in rs)} | {sum(x['ocr_pages'] for x in rs)} | - | {pct(t, 50):.2f} | {pct(t, 95):.2f} |")
    pt_text = [x["time_ms"] / 1000 / max(1, x["pages"]) for x in results if x["pages"] and x["ocr_pages"] == 0]
    pt_ocr = [x["time_ms"] / 1000 / max(1, x["pages"]) for x in results if x["ocr_pages"]]
    lines += ["", f"Seconds per page - text-layer PDF: p50 {pct(pt_text, 50):.3f}, p95 {pct(pt_text, 95):.3f}; "
                  f"OCR: p50 {pct(pt_ocr, 50):.2f}, p95 {pct(pt_ocr, 95):.2f}"]
    exp_ok = [x for x in results if x["expected_outcome"] != "fail"]
    lines += [f"Success rate (expected-good docs processed): {sum(x['status'] == 'ok' for x in exp_ok)}/{len(exp_ok)}",
              f"Corrupted files rejected cleanly: {sum(x['outcome_as_expected'] for x in results if x['expected_outcome'] == 'fail')}"
              f"/{sum(1 for x in results if x['expected_outcome'] == 'fail')}"]
    bad = [x for x in results if not x["outcome_as_expected"]]
    if bad:
        lines += ["", "## Not as expected", ""] + [f"- {x['doc_id']}: {x['status']} {x['error_code'] or ''} "
                                                   f"{x['error_message'] or ''} {x['warnings']}" for x in bad]
    fails = [x for x in results if x["status"] == "failed"]
    if fails:
        lines += ["", "## Failed documents (error codes)", ""] + [f"- {x['doc_id']}: {x['error_code']} - {x['error_message']}" for x in fails]
    (a.out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n" + "\n".join(lines))
    print(f"\nWrote {a.out / 'results.jsonl'}, {a.out / 'summary.md'} and text files in {a.out / 'text'}")


if __name__ == "__main__":
    main()
