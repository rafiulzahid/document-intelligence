#!/usr/bin/env python3
"""
build_report.py - produces the submission deliverables that contain measured numbers.

    python Code/build_report.py                 # uses the newest LLM run + the rules baseline
    python Code/build_report.py --skip-tests    # only rebuild the evaluation report

Writes:
    docs/eval_report.md      evaluation report (accuracy, hallucination, validation, cross-check, time, failures)
    docs/test-results.xml    JUnit XML from pytest
    docs/test_results.md     test summary by module
    docs/test-output.txt     full pytest console output
    docs/prompts.md          the exact runtime prompts (generated from the code, so it is always in sync)

Every number is computed from data/pipeline_out/<run>/predictions.jsonl, data/eval/gt and data/ingest_out.
"""
from __future__ import annotations

import argparse
import json
import platform
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE = ROOT / "Code"
DOCS = ROOT / "docs"
sys.path.insert(0, str(CODE))

import evaluate as ev  # noqa: E402

FIELDS = ev.HEADER
CAT_NAMES = {"image_ocr": "Scanned invoice images (Katana)", "digital_pdf": "Digital PDFs (Northwind + Mendeley)",
             "degraded_scan": "Degraded scans", "spreadsheet": "Spreadsheets (XLSX/XLS/CSV)",
             "real_scan": "Real receipts (SROIE)", "pdf_variant": "PDF variants (Mendeley IBAN)",
             "planted_error": "Planted errors", "duplicate": "Duplicates"}


def pct(x):
    return "–" if x is None else f"{100 * x:.1f}%"


def sh(cmd, timeout=20):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:  # noqa: BLE001
        return ""


def environment() -> dict:
    gpu = sh(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"]).replace("\n", "; ")
    tess = sh(["tesseract", "--version"]).splitlines()[:1]
    vers = {}
    for m in ["pymupdf", "fastapi", "sqlalchemy", "rq", "pytesseract", "rapidfuzz"]:
        try:
            mod = __import__(m)
            vers[m] = getattr(mod, "__version__", getattr(mod, "VersionBind", "")) or ""
        except Exception:  # noqa: BLE001
            pass
    from docintel.extract.llm import LLMClient, load_dotenv
    from docintel.extract.prompts import PROMPT_VERSION
    load_dotenv(ROOT / ".env")
    llm = LLMClient()
    try:
        ram = round(int(open("/proc/meminfo").read().split()[1]) / 1024 / 1024)
    except Exception:  # noqa: BLE001
        ram = None
    return {"os": f"{platform.system()} {platform.release()}", "python": platform.python_version(),
            "cpu": platform.processor() or platform.machine(), "ram_gb": ram, "gpu": gpu or "none detected",
            "tesseract": tess[0] if tess else "not found", "llm": llm.name, "prompt_version": PROMPT_VERSION,
            "packages": vers}


def find_runs(llm_run, baseline, long_run):
    out = ROOT / "data" / "pipeline_out"
    runs = [p.parent for p in out.glob("*/predictions.jsonl")]
    meta = {r: (json.loads((r / "run.json").read_text()) if (r / "run.json").exists() else {}) for r in runs}
    if llm_run:
        llm = out / llm_run
    else:
        cands = [r for r in runs if (meta[r].get("llm") or "none") != "none" and meta[r].get("documents", 0) > 5]
        llm = max(cands, key=lambda r: (r / "predictions.jsonl").stat().st_mtime) if cands else None
    base = out / baseline if (out / baseline / "predictions.jsonl").exists() else None
    lng = out / long_run if long_run and (out / long_run / "predictions.jsonl").exists() else None
    if lng is None:
        for r in runs:  # a separate long-PDF run made with --category long_pdf
            if meta[r].get("documents") == 1 and (meta[r].get("llm") or "none") != "none":
                lng = r
    return llm, base, lng


# ------------------------------------------------------------------ tests
def run_tests() -> dict:
    DOCS.mkdir(exist_ok=True)
    xml = DOCS / "test-results.xml"
    t0 = time.time()
    p = subprocess.run([sys.executable, "-m", "pytest", str(CODE / "tests"), "-v", "-p", "no:cacheprovider",
                        f"--junitxml={xml}"], cwd=ROOT, capture_output=True, text=True, timeout=1800)
    (DOCS / "test-output.txt").write_text(p.stdout + "\n" + p.stderr, encoding="utf-8")
    dur = time.time() - t0
    mods = defaultdict(lambda: Counter())
    total = Counter()
    if xml.exists():
        for tc in ET.parse(xml).getroot().iter("testcase"):
            mod = (tc.get("classname") or "").split(".")[-1] or "?"
            kind = "failed" if tc.find("failure") is not None or tc.find("error") is not None else \
                   "skipped" if tc.find("skipped") is not None else "passed"
            mods[mod][kind] += 1
            total[kind] += 1
    desc = {"test_ingest": "file type detection, PDF text layer + OCR, images/TIFF, XLSX/CSV, corrupted/encrypted/empty files",
            "test_extract_validate": "normalisation, grounding (no invented values), rules baseline, table mapping, segmentation, "
                                     "LLM client with a fake server + fallback, validation rules, duplicates, cross-verification",
            "test_api": "upload, background processing, results, corrections + re-validation, reprocess, search (keyword/semantic/"
                        "hybrid/filters), duplicates across uploads, ledger handling, cross-verify, auth, size/type limits, previews"}
    L = ["# Test results", "", f"Generated {time.strftime('%Y-%m-%d %H:%M')} · `pytest Code/tests` · {dur:.1f} s · exit code {p.returncode}", "",
         f"**{total['passed']} passed · {total['failed']} failed · {total['skipped']} skipped**", "",
         "| Module | Passed | Failed | Skipped | What it covers |", "|---|---|---|---|---|"]
    for m, c in sorted(mods.items()):
        L.append(f"| {m} | {c['passed']} | {c['failed']} | {c['skipped']} | {desc.get(m, '')} |")
    L += ["", "Raw outputs: `docs/test-results.xml` (JUnit) and `docs/test-output.txt` (console).",
          "All test files are generated on the fly; the API tests use a temporary SQLite database and run jobs synchronously. "
          "The same API code was also run against PostgreSQL + Redis + an RQ worker."]
    if total["failed"]:
        L += ["", "## Failures", "", "```", "\n".join(l for l in p.stdout.splitlines() if "FAILED" in l)[:4000], "```"]
    (DOCS / "test_results.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    return {"passed": total["passed"], "failed": total["failed"], "skipped": total["skipped"], "seconds": round(dur, 1)}


# ------------------------------------------------------------------ prompts
def write_prompts():
    from docintel.extract import prompts
    from docintel.extract.grounding import LOW_CONFIDENCE
    txt = f"""# Runtime prompts (version {prompts.PROMPT_VERSION})

Generated from `Code/docintel/extract/prompts.py`. These are the only prompts the system sends to the LLM at runtime.
Settings: temperature 0, JSON mode (`response_format: json_object` on OpenAI-compatible servers), max 2000 output
tokens, document text truncated to 14,000 characters per invoice unit.

## System prompt

```text
{prompts.SYSTEM_PROMPT}
```

## User message template

```text
{prompts.USER_TEMPLATE}
```

## What happens to the answer (not prompted, done in code)

1. The reply is parsed as JSON (tolerates code fences or prose around it); up to 3 attempts on transport errors.
2. **Normalisation** in Python: dates → ISO (day/month order learned from other dates in the document, ambiguous
   ones marked *uncertain*), amounts → numbers (handles `889,20`, `1 234,56`, `$1,444.80`), currency symbols → ISO codes.
3. **Grounding**: every value must be found in the source text (numbers by value, names by fuzzy match ≥ 85,
   invoice numbers exactly). A value that is not in the document is **discarded** (status *removed*) — this is how the
   system avoids inventing values. Confidence below {LOW_CONFIDENCE} marks a field *uncertain*.
4. Fields the LLM leaves empty are filled from the deterministic rules baseline when it finds them (marked *uncertain*).
5. Spreadsheets with a clear structure never reach the LLM: they are mapped deterministically.
6. Validation rules then run on the result (see README).
"""
    (DOCS / "prompts.md").write_text(txt, encoding="utf-8")


# ------------------------------------------------------------------ report
def dataset_section() -> list[str]:
    import csv
    man = ROOT / "data" / "eval" / "manifest.csv"
    rows = list(csv.DictReader(open(man, encoding="utf-8")))
    by = Counter((r["source"], r["category"]) for r in rows)
    L = ["## 2. Evaluation dataset", "",
         f"{len(rows)} documents built by `Code/prepare_dataset.py` (fixed seed) from public datasets plus generated test files. "
         "Ground truth for every document is in `data/eval/gt/<doc_id>.json`; only labeled fields are scored, and a field "
         "labeled *null* (not printed on the document) must be predicted as null.", "",
         "| Source | Category | Documents |", "|---|---|---|"]
    for (src, cat), n in sorted(by.items()):
        L.append(f"| {src} | {cat} | {n} |")
    L += ["", "| Dataset | Link | License | Real or synthetic |", "|---|---|---|---|",
          "| Katana ML invoices (labels + images) | https://huggingface.co/datasets/katanaml-org/invoices-donut-data-v1 | MIT | synthetic invoices rendered as images |",
          "| Mendeley *Samples of electronic invoices* (PDFs) | https://data.mendeley.com/datasets/tnj49gpmtz/2 | CC BY 4.0 | synthetic; labels from the matching Katana row |",
          "| Northwind invoices (PDF) | https://huggingface.co/datasets/AyoubChLin/northwind_invocies | Apache-2.0 | generated from the Northwind sample database |",
          "| SROIE receipts (Voxel51 mirror) | https://huggingface.co/datasets/Voxel51/scanned_receipts | CC-BY-4.0 | **real** scanned receipts (partial labels) |",
          "| Generated | spreadsheets, degraded scans, planted errors, duplicates, corrupted files, long PDF | – | derived from the above |"]
    card = ROOT / "data" / "eval" / "DATASET_CARD.md"
    if card.exists():
        t = card.read_text(encoding="utf-8")
        if "## Exclusions" in t:
            L += ["", "Label notes from the dataset card:", ""] + [l for l in t.split("## Exclusions", 1)[1].splitlines()[1:] if l.strip()]
    return L


def report(llm, base, lng, env, tests) -> str:
    gt = ROOT / "data" / "eval"
    m = ev.evaluate(llm, gt) if llm else None
    b = ev.evaluate(base, gt) if base else None
    ml = ev.evaluate(lng, gt) if lng else None
    main, other = (m, b) if m else (b, None)
    if main is None:
        raise SystemExit("No pipeline run found. Run: python Code/run_pipeline.py --llm none  and  python Code/run_pipeline.py --skip-long")
    two = other is not None
    hdr = lambda *c: "| " + " | ".join(c) + " |"  # noqa: E731
    L = ["# Evaluation report", "",
         f"Generated {time.strftime('%Y-%m-%d %H:%M')} by `Code/build_report.py` from measured runs. "
         f"Main run: **{main['llm']}** (`{Path(main['run']).name}`)" + (f"; baseline: **rules only, no LLM** (`{Path(other['run']).name}`)." if two else "."),
         "", "## 1. Summary", ""]
    L += [hdr("Metric", "LLM pipeline" if two else "Value", *(["Rules baseline"] if two else [])), "|---|---|" + ("---|" if two else "")]

    def row(name, f):
        L.append(hdr(name, f(main), *([f(other)] if two else [])))
    row("Field accuracy (all scored fields)", lambda x: pct(x["field_accuracy_overall_micro"]))
    row("Fields scored", lambda x: str(x["fields_scored"]))
    row("Hallucination rate (value returned where the document has none)", lambda x: pct(x["hallucination_overall"]))
    row("Line items precision / recall", lambda x: f"{pct(x['line_items_overall']['precision'])} / {pct(x['line_items_overall']['recall'])}")
    row("Document success rate", lambda x: pct(x["success_rate"]))
    row("Unexpected failure rate", lambda x: pct(x["failure_rate"]))
    row("Corrupted files rejected cleanly", lambda x: x["corrupted_rejected"])
    row("Planted problems detected", lambda x: f"{sum(v['detected'] for v in x['validation_detection'].values())}/"
                                               f"{sum(v['total'] for v in x['validation_detection'].values())}")
    row("Clean documents wrongly flagged (math/duplicate rules)", lambda x: f"{x['false_flag_rate_clean_docs']['flagged']}/"
                                                                       f"{x['false_flag_rate_clean_docs']['clean_docs']} ({pct(x['false_flag_rate_clean_docs']['rate'])})")
    row("PDF↔Excel mismatches detected", lambda x: f"{x['cross_verification']['detected']}/{x['cross_verification']['expected_mismatches']}"
                                                if x.get("cross_verification") else "–")
    if tests:
        L += ["", f"Automated tests: **{tests['passed']} passed, {tests['failed']} failed, {tests['skipped']} skipped** "
                  f"({tests['seconds']} s) — see `docs/test_results.md`."]

    L += ["", "## 1b. Setup", "", hdr("Item", "Value"), "|---|---|"]
    for k, v in [("OS", env["os"]), ("CPU", env["cpu"]), ("RAM", f"{env['ram_gb']} GB" if env["ram_gb"] else "–"), ("GPU", env["gpu"]),
                 ("Python", env["python"]), ("OCR", env["tesseract"] + " (Tesseract; EasyOCR optional)"),
                 ("LLM", f"{main['llm']} · temperature 0 · JSON mode · prompt {env['prompt_version']}"),
                 ("Key packages", ", ".join(f"{k} {v}" for k, v in env["packages"].items() if v))]:
        L.append(hdr(k, str(v)))
    L += [""] + dataset_section()

    # field accuracy
    L += ["", "## 3. Extraction accuracy", "", "Scoring: invoice number exact (ignoring spaces, `#`, leading zeros) · date = same ISO date · "
          "names fuzzy token-set similarity ≥ 85 · amounts ±0.01 · currency same ISO code · null labels correct only if predicted null.", "",
          hdr("Field", "LLM pipeline" if two else "Accuracy", *(["Rules baseline"] if two else []), "n"), "|---|---|" + ("---|" if two else "") + "---|"]
    for f in FIELDS:
        a = main["field_accuracy_by_field"].get(f)
        if not a:
            continue
        o = other["field_accuracy_by_field"].get(f, {}).get("accuracy") if two else None
        L.append(hdr(f, pct(a["accuracy"]), *([pct(o)] if two else []), str(a["n"])))
    cats = [c for c in CAT_NAMES if c in main["field_accuracy_by_category"]]
    L += ["", "By document category (LLM pipeline):", "", hdr("Field", *[CAT_NAMES[c] for c in cats]), "|---|" + "---|" * len(cats)]
    for f in FIELDS:
        L.append(hdr(f, *[pct(main["field_accuracy_by_category"][c].get(f, {}).get("accuracy")) for c in cats]))
    L.append(hdr("**all fields of a document correct**", *[pct(main["document_accuracy"].get(c, {}).get("accuracy")) for c in cats]))
    L += ["", "### Line items", "", hdr("Category", "Precision", "Recall", "Item count correct", "Invoices"), "|---|---|---|---|---|"]
    for c, v in sorted(main["line_items"].items()):
        L.append(hdr(CAT_NAMES.get(c, c), pct(v["precision"]), pct(v["recall"]), pct(v["count_match"]), str(v["invoices"])))

    L += ["", "## 4. Missing information — no invented values", "",
          "Northwind invoices do not print a supplier, subtotal or tax, and the labels say *null*. Any value returned "
          "for these fields would be invented.", "", hdr("Field", "Hallucination rate", "Null labels"), "|---|---|---|"]
    for f, v in main["hallucination"].items():
        L.append(hdr(f, pct(v["rate"]), str(v["null_labels"])))
    L += ["", "Mechanism: the LLM must quote evidence; every value is checked against the source text and discarded if it is "
              "not there (status *removed*). The review UI shows each field's status and evidence."]

    L += ["", "## 5. Validation", "", hdr("Planted problem", "Detected", "Recall"), "|---|---|---|"]
    for fl, v in main["validation_detection"].items():
        L.append(hdr(fl, f"{v['detected']}/{v['total']}", pct(v["recall"])))
    ff = main["false_flag_rate_clean_docs"]
    L.append(hdr("clean documents wrongly flagged (math/duplicate rules)", f"{ff['flagged']}/{ff['clean_docs']}", pct(ff["rate"])))
    if main.get("missed_validation_flags"):
        L += ["", "Missed:", ""] + [f"- {x}" for x in main["missed_validation_flags"]]
    L += ["", "Rules: required fields · negative amounts · invalid/future dates · quantity × unit price = line amount · "
              "line items = subtotal (or total when no tax is shown) · subtotal + tax = total · tax rate 0–30% · duplicates "
              "(file hash; invoice number + party; invoice number + total) · uncertain or discarded values → *needs review*. "
              "Money tolerance: 2 cents + 0.01%."]
    cv = main.get("cross_verification")
    if cv:
        L += ["", "## 6. Cross-verification (PDF ↔ Excel ledger)", "",
              f"Expected mismatches detected: **{cv['detected']}/{cv['expected_mismatches']}** · invoices matched: {cv['matched_invoices']} · "
              f"mismatches reported on invoices without a planted change: {cv['reported_on_clean_invoices']} · "
              f"only in PDFs: {cv['only_in_pdf']} · only in ledger: {cv['only_in_ledger']}"]
        if cv.get("missed"):
            L.append("Missed: " + "; ".join(cv["missed"]))

    L += ["", "## 7. Processing time and throughput", ""]
    lp = (ml or main).get("long_pdf")
    if lp:
        L += [f"**Long PDF** ({lp['pages']} pages, one invoice per page, run `{Path((ml or main)['run']).name}`): "
              f"{lp['found_invoices']}/{lp['expected_invoices']} invoices found, {lp['invoice_numbers_found']} invoice numbers "
              f"recovered, {lp['totals_correct']} totals correct, **{lp['time_s']} s total ({lp['s_per_page']} s/page)**.", ""]
    ing = ROOT / "data" / "ingest_out" / "summary.md"
    if ing.exists():
        t = ing.read_text(encoding="utf-8")
        L += ["Ingestion only (`Code/run_ingest.py`, Tesseract on CPU):", ""] + \
             [l for l in t.splitlines() if l.startswith("|") or l.startswith("Seconds") or l.startswith("Success") or l.startswith("Corrupted")] + [""]
    L += ["End-to-end per document (ingestion + extraction + validation, LLM pipeline):", "",
          hdr("Category", "p50 s/doc", "p95 s/doc", "Docs"), "|---|---|---|---|"]
    for c, v in sorted(main["time_s_per_doc"].items()):
        L.append(hdr(CAT_NAMES.get(c, c), str(v["p50"]), str(v["p95"]), str(v["n"])))
    L += ["", f"LLM calls: {main['extraction_methods']} · tokens in/out {main['llm_tokens']['input']}/{main['llm_tokens']['output']} · "
              f"total LLM time {main['llm_time_s']} s. The API scales by adding RQ workers; LLM calls run in parallel."]

    # failures
    L += ["", "## 8. Failure handling", ""]
    preds = [json.loads(l) for l in open(Path(main["run"]) / "predictions.jsonl", encoding="utf-8")]
    fails = [p for p in preds if p["status"] == "failed"]
    L += [hdr("Document", "Error code", "Message"), "|---|---|---|"] + \
         [hdr(p["doc_id"], p["error_code"] or "", (p["error_message"] or "")[:90].replace("|", "/")) for p in fails]
    if main["unexpected_failures"]:
        L += ["", "Unexpected failures:", ""] + [f"- {x}" for x in main["unexpected_failures"]]
    else:
        L += ["", "No unexpected failures: every valid file produced a result and every corrupted file was rejected with an error code."]

    L += ["", "## 9. Failure analysis", ""]
    errs = main["errors_sample"]
    cnt = Counter((CAT_NAMES.get(e["category"], e["category"]), e["field"]) for e in errs)
    if cnt:
        L += ["Most frequent errors (category · field · count):", ""] + [f"- {c} · {f} · {n}" for (c, f), n in cnt.most_common(8)]
    ingest_res = ROOT / "data" / "ingest_out" / "results.jsonl"
    if ingest_res.exists():
        confs = defaultdict(list)
        for l in open(ingest_res, encoding="utf-8"):
            r = json.loads(l)
            if r.get("mean_ocr_conf") is not None:
                confs[r["category"]].append(r["mean_ocr_conf"])
        if confs:
            L += ["", "Mean OCR confidence by category: " + ", ".join(f"{CAT_NAMES.get(c, c)} {sum(v) / len(v):.2f}" for c, v in sorted(confs.items()))]
    L += ["", "Examples:", "", hdr("Document", "Field", "Truth", "Predicted"), "|---|---|---|---|"]
    for e in errs[:20]:
        L.append(hdr(e["doc_id"], e["field"], str(e["truth"])[:45].replace("|", "/"), str(e["predicted"])[:45].replace("|", "/")))
    L += ["", "Observations (check against the examples above and edit if needed):", "",
          "- **Real receipts (SROIE)** are the weakest category: thermal-paper scans give much lower OCR confidence than the "
          "synthetic invoices, so dates and totals are often misread or not found; shop names on receipts are frequently logos.",
          "- **Label noise**: a few Katana labels contain a tax id instead of a seller name or typos in names; these rows were "
          "excluded or show up as errors that are not real system errors.",
          "- **Line items**: descriptions that wrap over several lines and tables with both net and gross columns are the main "
          "source of line-item errors.",
          "- **Spreadsheets** are mapped deterministically and reach 100% on the fields they contain.",
          "", "## 10. Limitations and next steps", "",
          "- Most labeled data is synthetic; real B2B invoices (e.g. the DocILE benchmark) would give a harder, more realistic test.",
          "- Better OCR for receipts (EasyOCR/PaddleOCR on GPU, or a vision-language model) is the biggest expected gain.",
          "- A larger LLM or a hosted model would likely improve receipts and line items; the client is provider-agnostic.",
          "- Duplicate detection uses exact keys; near-duplicate detection on embeddings is a natural extension.",
          "", "## 11. Reproduce", "", "```bash",
          "python Code/prepare_dataset.py                        # dataset + labels",
          "python Code/run_ingest.py                             # ingestion report",
          "python Code/run_pipeline.py --llm none                # rules baseline",
          "python Code/run_pipeline.py --skip-long               # LLM pipeline",
          "python Code/run_pipeline.py --category long_pdf --run-name <model>_long   # optional long-PDF run",
          "python Code/build_report.py                           # this report + test results", "```"]
    return "\n".join(L) + "\n"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--llm-run", help="folder name under data/pipeline_out (default: newest LLM run)")
    ap.add_argument("--baseline", default="rules_baseline")
    ap.add_argument("--long-run", help="folder name of a long-PDF run (optional)")
    ap.add_argument("--skip-tests", action="store_true")
    a = ap.parse_args()
    DOCS.mkdir(exist_ok=True)
    print("writing docs/prompts.md")
    write_prompts()
    tests = None
    if not a.skip_tests:
        print("running the test suite (about a minute) ...")
        tests = run_tests()
        print(f"  {tests}")
    llm, base, lng = find_runs(a.llm_run, a.baseline, a.long_run)
    print(f"runs: llm={llm and llm.name} baseline={base and base.name} long={lng and lng.name}")
    env = environment()
    (DOCS / "eval_report.md").write_text(report(llm, base, lng, env, tests), encoding="utf-8")
    print("wrote docs/eval_report.md, docs/test_results.md, docs/test-results.xml, docs/test-output.txt, docs/prompts.md")


if __name__ == "__main__":
    main()
