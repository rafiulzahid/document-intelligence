#!/usr/bin/env python3
"""
run_pipeline.py - ingest -> extract -> validate every document in the evaluation set.

    python Code/run_pipeline.py                     # uses the LLM from .env (or the rules baseline)
    python Code/run_pipeline.py --llm none          # rules baseline only (no LLM)
    python Code/run_pipeline.py --skip-long         # skip the ~830-page PDF (830 LLM calls)
    python Code/run_pipeline.py --file x.pdf        # one file, prints the result

Ingestion results are cached in data/pipeline_out/ingest_cache/, so re-running after changing the
prompt or the LLM does not repeat OCR. Writes data/pipeline_out/<run>/predictions.jsonl and a
per-document JSON; then run Code/evaluate.py on that folder.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from docintel.crossverify import cross_verify  # noqa: E402
from docintel.extract.llm import LLMClient, load_dotenv  # noqa: E402
from docintel.extract.schema import ExtractedInvoice  # noqa: E402
from docintel.ingest import ingest_file  # noqa: E402
from docintel.ingest.types import IngestResult  # noqa: E402
from docintel.pipeline import process_file, process_ingested  # noqa: E402
from docintel.validate import find_duplicates, review_status  # noqa: E402


def main():
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", type=Path, default=ROOT / "data" / "eval" / "manifest.csv")
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "pipeline_out")
    ap.add_argument("--run-name", help="sub-folder name (default: the LLM name)")
    ap.add_argument("--llm", help="'none' for the rules baseline; otherwise taken from .env")
    ap.add_argument("--model", help="override DOCINTEL_LLM_MODEL")
    ap.add_argument("--ocr", default="auto")
    ap.add_argument("--category")
    ap.add_argument("--skip-long", action="store_true")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--doc-workers", type=int, default=4, help="documents sent to the LLM in parallel")
    ap.add_argument("--file", type=Path)
    a = ap.parse_args()

    llm = LLMClient(provider=a.llm, model=a.model) if a.llm != "none" else LLMClient(provider="none")
    print(f"LLM: {llm.name}" + ("" if llm.enabled else "  (rules baseline; configure .env to use an LLM)"))

    if a.file:
        rec = process_file(a.file, llm=llm, ocr=a.ocr)
        print(json.dumps(rec, indent=2, ensure_ascii=False, default=str))
        return

    run = a.run_name or ("rules_baseline" if not llm.enabled else llm.model.replace("/", "_").replace(":", "_"))
    out = a.out / run
    cache = a.out / "ingest_cache"
    (out / "docs").mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    base = a.manifest.parent
    rows = list(csv.DictReader(open(a.manifest, encoding="utf-8")))
    if a.category:
        rows = [r for r in rows if r["category"] == a.category]
    if a.skip_long:
        rows = [r for r in rows if r["category"] != "long_pdf"]
    if a.limit:
        rows = rows[: a.limit]

    # pass 1: ingestion (cached; sequential so OCR does not fight for the CPU/GPU)
    t_all = time.perf_counter()
    ingested = []
    for row in rows:
        cp = cache / f"{row['doc_id']}.json"
        if cp.exists():
            res = IngestResult.from_dict(json.loads(cp.read_text(encoding="utf-8")))
        else:
            res = ingest_file(base / row["file"], ocr=a.ocr)
            cp.write_text(json.dumps(res.to_dict(), ensure_ascii=False), encoding="utf-8")
        ingested.append((row, res))

    # pass 2: extraction + validation, several documents in parallel when an LLM is used
    def work(item):
        row, res = item
        rec = process_ingested(res, llm)
        rec.update(doc_id=row["doc_id"], category=row["category"], source=row["source"])
        rec["timings_s"]["total"] = round(rec["timings_s"]["ingest"] + rec["timings_s"]["extract_validate"], 3)
        return rec

    from concurrent.futures import ThreadPoolExecutor
    records, done = [None] * len(ingested), 0
    with ThreadPoolExecutor(max_workers=a.doc_workers if llm.enabled else 1) as pool:
        futs = {pool.submit(work, it): k for k, it in enumerate(ingested)}
        from concurrent.futures import as_completed
        for f in as_completed(futs):
            k = futs[f]; rec = f.result(); records[k] = rec; done += 1
            print(f"[{done:3d}/{len(rows)}] {rec['doc_id']:36s} {rec['status']:13s} invoices={len(rec['invoices']):<4d} "
                  f"method={','.join(rec['extraction'].get('methods', {}).keys()) or '-':22s} "
                  f"{rec['timings_s']['extract_validate']:6.2f}s", flush=True)

    # duplicates across the whole batch (in processing order)
    batch = [(f"{r['doc_id']}#{i['index']}", ExtractedInvoice.from_dict(i["invoice"]), r["sha256"])
             for r in records for i in r["invoices"]]
    dups = find_duplicates(batch)
    for r in records:
        for i in r["invoices"]:
            extra = dups.get(f"{r['doc_id']}#{i['index']}", [])
            if extra:
                i["issues"] += extra
                i["status"] = review_status(i["issues"])
        if r["invoices"]:
            r["status"] = "needs_review" if any(i["status"] == "needs_review" for i in r["invoices"]) else "valid"

    # PDF <-> Excel cross-verification for every ledger document in the set
    gt_dir = base / "gt"
    for r in records:
        if r["category"] != "cross_verify":
            continue
        gt = json.loads((gt_dir / f"{r['doc_id']}.json").read_text(encoding="utf-8"))
        targets = set(gt.get("cross_verify_against", []))
        pdf_invs = [ExtractedInvoice.from_dict(i["invoice"]) for x in records if x["doc_id"] in targets for i in x["invoices"]]
        ledger = [ExtractedInvoice.from_dict(i["invoice"]) for i in r["invoices"]]
        r["cross_verification"] = cross_verify(pdf_invs, ledger, "pdf", "ledger")
        cv = r["cross_verification"]
        print(f"\ncross-verify {r['doc_id']}: matched {cv['matched']}, mismatches {len(cv['mismatches'])}, "
              f"only in PDFs {len(cv['only_in_pdf'])}, only in ledger {len(cv['only_in_ledger'])}")

    with open(out / "predictions.jsonl", "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
            (out / "docs" / f"{r['doc_id']}.json").write_text(json.dumps(r, indent=2, ensure_ascii=False, default=str),
                                                               encoding="utf-8")
    meta = {"llm": llm.name, "run": run, "documents": len(records), "wall_s": round(time.perf_counter() - t_all, 1),
            "created": time.strftime("%Y-%m-%d %H:%M:%S")}
    (out / "run.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    st = {}
    for r in records:
        st[r["status"]] = st.get(r["status"], 0) + 1
    print(f"\nstatus counts: {st}\nwall time {meta['wall_s']} s\nWrote {out}/predictions.jsonl\n"
          f"Next: python Code/evaluate.py --run {out}")


if __name__ == "__main__":
    main()
