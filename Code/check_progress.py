#!/usr/bin/env python3
"""
check_progress.py - checks that steps 1-5 (and the 4b fixes) are in place before starting step 6.

    python Code/check_progress.py            # full check, runs the test suite too
    python Code/check_progress.py --no-tests # skip pytest (faster)

Prints PASS / WARN / FAIL lines and a verdict. Paste the whole output into the chat.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE = ROOT / "Code"
sys.path.insert(0, str(CODE))
results = []


def rec(level, msg):
    results.append((level, msg))
    print(f"[{level}] {msg}", flush=True)


def section(t):
    print(f"\n== {t} ==", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-tests", action="store_true")
    a = ap.parse_args()

    section("Environment")
    rec("PASS" if sys.version_info >= (3, 10) else "FAIL", f"Python {sys.version.split()[0]}")
    in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    rec("PASS" if in_venv else "WARN", f"virtual environment active: {in_venv} ({sys.prefix})")
    missing = []
    for mod in ["pymupdf", "pypdf", "PIL", "numpy", "openpyxl", "xlrd", "pandas", "charset_normalizer",
                "dateutil", "pytesseract", "rapidfuzz", "pytest"]:
        try:
            importlib.import_module(mod)
        except Exception:  # noqa: BLE001
            missing.append(mod)
    rec("PASS" if not missing else "FAIL", "Python packages installed" + (f" - missing: {missing} "
        "(pip install -r Code/requirements.txt)" if missing else ""))
    rec("PASS" if shutil.which("tesseract") else "FAIL", "Tesseract OCR installed" if shutil.which("tesseract")
        else "Tesseract missing (sudo apt install -y tesseract-ocr)")

    section("Code (steps 3-5) and the 4b fix pack")
    need = ["prepare_dataset.py", "run_ingest.py", "run_pipeline.py", "evaluate.py", "requirements.txt",
            "docintel/pipeline.py", "docintel/validate.py", "docintel/crossverify.py", "docintel/normalize.py",
            "docintel/ingest/__init__.py", "docintel/ingest/pdf.py", "docintel/ingest/image.py", "docintel/ingest/sheet.py",
            "docintel/extract/__init__.py", "docintel/extract/llm.py", "docintel/extract/grounding.py",
            "docintel/extract/rules.py", "docintel/extract/tables.py", "tests/test_ingest.py", "tests/test_extract_validate.py"]
    absent = [f for f in need if not (CODE / f).exists()]
    rec("PASS" if not absent else "FAIL", "all code files present" + (f" - missing: {absent}" if absent else ""))
    fixes = {}
    try:
        from docintel.extract import prompts
        fixes["prompt v2"] = prompts.PROMPT_VERSION == "v2"
    except Exception:  # noqa: BLE001
        fixes["prompt v2"] = False
    try:
        from docintel import validate
        fixes["2-cent tolerance"] = abs(validate.ABS_TOL - 0.02) < 1e-9
    except Exception:  # noqa: BLE001
        fixes["2-cent tolerance"] = False
    fixes["parallel LLM runs"] = "--doc-workers" in (CODE / "run_pipeline.py").read_text(encoding="utf-8") if (CODE / "run_pipeline.py").exists() else False
    fixes["rules fill LLM gaps"] = "FILLABLE" in (CODE / "docintel/extract/__init__.py").read_text(encoding="utf-8") if (CODE / "docintel/extract/__init__.py").exists() else False
    ok4b = all(fixes.values())
    rec("PASS" if ok4b else "FAIL", "4b fix pack applied: " + ", ".join(f"{k}={'yes' if v else 'NO'}" for k, v in fixes.items())
        + ("" if ok4b else "  -> unzip Code_step4b_fixes.zip over the project"))

    section("Evaluation data (steps 1-2)")
    man = ROOT / "data/eval/manifest.csv"
    if man.exists():
        import csv
        rows = list(csv.DictReader(open(man, encoding="utf-8")))
        cats = {}
        for r in rows:
            cats[r["category"]] = cats.get(r["category"], 0) + 1
        rec("PASS" if len(rows) >= 200 else "WARN", f"data/eval/manifest.csv: {len(rows)} documents {cats}")
        gt_n = len(list((ROOT / "data/eval/gt").glob("*.json")))
        rec("PASS" if gt_n == len(rows) else "FAIL", f"ground-truth files: {gt_n}")
        sheets = [json.loads(p.read_text(encoding="utf-8")) for p in (ROOT / "data/eval/gt").glob("csv_*.json")]
        relabeled = bool(sheets) and all(i.get("currency") is None for g in sheets for i in g["invoices"])
        rec("PASS" if relabeled else "FAIL", "labels regenerated after 4b (spreadsheet currency = null)" if relabeled
            else "labels are from before 4b -> run: python Code/prepare_dataset.py")
        miss = [r["doc_id"] for r in rows if not (ROOT / "data/eval" / r["file"]).exists()]
        rec("PASS" if not miss else "FAIL", "all documents present on disk" + (f" - missing {len(miss)}, e.g. {miss[:3]}" if miss else ""))
    else:
        rec("FAIL", "data/eval/manifest.csv not found -> run python Code/prepare_dataset.py")
        rows = []

    section("Pipeline runs (steps 3-5)")
    cache = ROOT / "data/pipeline_out/ingest_cache"
    nc = len(list(cache.glob("*.json"))) if cache.exists() else 0
    rec("PASS" if rows and nc >= len(rows) - 1 else "WARN", f"OCR/ingestion cache: {nc} documents")
    runs = sorted((p.parent for p in (ROOT / "data/pipeline_out").glob("*/predictions.jsonl")), key=lambda p: p.stat().st_mtime)
    if not runs:
        rec("FAIL", "no pipeline run found -> python Code/run_pipeline.py --llm none, then python Code/run_pipeline.py --skip-long")
    man_time = man.stat().st_mtime if man.exists() else 0
    for run in runs:
        pred_t = (run / "predictions.jsonl").stat().st_mtime
        meta = json.loads((run / "run.json").read_text()) if (run / "run.json").exists() else {}
        n = sum(1 for _ in open(run / "predictions.jsonl", encoding="utf-8"))
        fresh = pred_t >= man_time
        rec("PASS" if fresh else "WARN", f"run '{run.name}': {n} documents, LLM={meta.get('llm')}, {meta.get('created')}"
            + ("" if fresh else "  (older than the current labels/code -> rerun it)"))
        mp = run / "metrics.json"
        if mp.exists() and mp.stat().st_mtime >= pred_t:
            m = json.loads(mp.read_text())
            vd = m.get("validation_detection", {})
            cv = m.get("cross_verification") or {}
            rec("INFO", f"   field accuracy {pct(m.get('field_accuracy_overall_micro'))} · hallucination "
                f"{pct(m.get('hallucination_overall'))} · line items P/R {pct(m['line_items_overall']['precision'])}/"
                f"{pct(m['line_items_overall']['recall'])} · success {pct(m.get('success_rate'))} · corrupted rejected "
                f"{m.get('corrupted_rejected')}")
            rec("INFO", "   validation: " + ", ".join(f"{k} {v['detected']}/{v['total']}" for k, v in vd.items())
                + f" · false flags {m['false_flag_rate_clean_docs']['flagged']}/{m['false_flag_rate_clean_docs']['clean_docs']}"
                + (f" · cross-check {cv.get('detected')}/{cv.get('expected_mismatches')}" if cv else ""))
        else:
            rec("WARN", f"   not evaluated since the last run -> python Code/evaluate.py --run data/pipeline_out/{run.name}")
    has_llm_run = any((json.loads((r / "run.json").read_text()).get("llm") or "none") != "none"
                      for r in runs if (r / "run.json").exists())
    rec("PASS" if has_llm_run else "WARN", "an LLM run exists" if has_llm_run else "only the rules baseline was run")

    section("LLM configuration")
    from docintel.extract.llm import LLMClient, load_dotenv
    load_dotenv(ROOT / ".env")
    rec("PASS" if (ROOT / ".env").exists() else "WARN", ".env present" if (ROOT / ".env").exists()
        else ".env missing -> cp .env.example .env")
    llm = LLMClient()
    rec("PASS" if llm.enabled else "WARN", f"LLM configured: {llm.name}")
    if llm.enabled and llm.provider == "openai":
        try:
            req = urllib.request.Request(llm.base_url + "/models", headers={"authorization": f"Bearer {llm.api_key}"} if llm.api_key else {})
            with urllib.request.urlopen(req, timeout=5) as r:
                ids = [x.get("id") for x in json.load(r).get("data", [])]
            found = any(llm.model == i or i.startswith(llm.model) for i in ids)
            rec("PASS" if found else "WARN", f"LLM server reachable at {llm.base_url}; model '{llm.model}' "
                + ("available" if found else f"NOT listed (have: {ids[:6]})"))
        except Exception as e:  # noqa: BLE001
            rec("WARN", f"LLM server not reachable at {llm.base_url} ({type(e).__name__}) -> start it: ollama serve")

    if not a.no_tests:
        section("Tests")
        p = subprocess.run([sys.executable, "-m", "pytest", str(CODE / "tests"), "-q", "-p", "no:cacheprovider"],
                           cwd=ROOT, capture_output=True, text=True, timeout=900)
        last = (p.stdout.strip().splitlines() or ["(no output)"])[-1]
        rec("PASS" if p.returncode == 0 else "FAIL", f"pytest: {last}")
        if p.returncode != 0:
            print("\n".join(p.stdout.splitlines()[-25:]))

    section("Verdict")
    fails = [m for l, m in results if l == "FAIL"]
    warns = [m for l, m in results if l == "WARN"]
    if not fails and not warns:
        print("READY FOR STEP 6: everything from steps 1-5 and the 4b fixes is in place.")
    elif not fails:
        print("READY FOR STEP 6, with warnings (fix them when convenient):")
        for w in warns:
            print("  -", w)
    else:
        print("NOT READY yet. Fix these first:")
        for f in fails:
            print("  -", f)
        for w in warns:
            print("  - (warning)", w)


def pct(x):
    return "-" if x is None else f"{100 * x:.1f}%"


if __name__ == "__main__":
    main()
