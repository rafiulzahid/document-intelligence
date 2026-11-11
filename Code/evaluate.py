#!/usr/bin/env python3
"""
evaluate.py - score a pipeline run against the ground truth and write the evaluation report.

    python Code/evaluate.py --run data/pipeline_out/rules_baseline
    python Code/evaluate.py --run data/pipeline_out/qwen2.5_7b-instruct --compare data/pipeline_out/rules_baseline

Writes <run>/metrics.json and <run>/eval_report.md. Every number comes from predictions.jsonl and
data/eval/gt/*.json: nothing is estimated.

Scoring rules (also printed in the report):
  invoice_number  exact after removing spaces, '#', leading zeros (case-insensitive)
  invoice_date    equal ISO date
  names           fuzzy token-set similarity >= 85 (labels in Katana include the address)
  amounts         |predicted - truth| <= 0.01
  currency        equal ISO code
  null labels     correct only if the prediction is null (counts towards the hallucination rate)
  line items      matched by amount (+-0.01) and description similarity >= 70 -> precision / recall
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from docintel.normalize import norm_id, norm_text  # noqa: E402

try:
    from rapidfuzz import fuzz
    def sim(a, b): return fuzz.token_set_ratio(norm_text(a), norm_text(b))  # noqa: E704
except ImportError:  # pragma: no cover
    from difflib import SequenceMatcher
    def sim(a, b):  # noqa: E704
        a, b = norm_text(a), norm_text(b)
        if a and (a in b or b in a):
            return 100.0
        return 100 * SequenceMatcher(None, a, b).ratio()

HEADER = ["invoice_number", "invoice_date", "supplier_name", "customer_name", "subtotal", "tax", "total", "currency"]
FIELD_CATS = ["digital_pdf", "image_ocr", "degraded_scan", "spreadsheet", "real_scan", "pdf_variant", "planted_error", "duplicate"]
FLAG_RULES = {"total_mismatch": {"LINE_SUM_MISMATCH", "TOTAL_MISMATCH"}, "line_item_math": {"LINE_ITEM_MATH"},
              "duplicate": {"DUPLICATE"}}
MATH_RULES = {"LINE_ITEM_MATH", "LINE_SUM_MISMATCH", "TOTAL_MISMATCH", "DUPLICATE"}


def pct(vals, q):
    if not vals:
        return None
    v = sorted(vals)
    return v[max(0, min(len(v) - 1, round(q / 100 * (len(v) - 1))))]


def field_correct(f, pred, gt):
    if gt is None:
        return pred in (None, "")
    if pred in (None, ""):
        return False
    if f == "invoice_number":
        return norm_id(pred) == norm_id(gt)
    if f in ("subtotal", "tax", "total"):
        try:
            return abs(float(pred) - float(gt)) <= 0.011
        except (TypeError, ValueError):
            return False
    if f in ("supplier_name", "customer_name"):
        return sim(pred, gt) >= 85
    return str(pred).strip().upper() == str(gt).strip().upper()


def line_item_scores(pred_items, gt_items):
    used, tp = set(), 0
    for g in gt_items:
        for k, p in enumerate(pred_items):
            if k in used or p.get("amount") is None or g.get("amount") is None:
                continue
            if abs(p["amount"] - g["amount"]) <= 0.011 and (not g.get("description") or not p.get("description")
                                                            or sim(p["description"], g["description"]) >= 70):
                used.add(k); tp += 1; break
    return tp, len(pred_items), len(gt_items)


def pair_invoices(pred_invs, gt_invs):
    """Match predicted invoices to ground-truth invoices (by invoice number, else in order)."""
    pairs, free = [], list(range(len(pred_invs)))
    for g in gt_invs:
        j = None
        if g.get("invoice_number"):
            j = next((k for k in free if norm_id(pred_invs[k]["invoice"].get("invoice_number")) == norm_id(g["invoice_number"])), None)
        if j is None and len(gt_invs) == 1 and free:
            j = free[0]
        if j is not None:
            free.remove(j)
        pairs.append((g, pred_invs[j]["invoice"] if j is not None else None))
    return pairs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", type=Path, help="folder with predictions.jsonl (default: newest run)")
    ap.add_argument("--gt", type=Path, default=ROOT / "data" / "eval")
    ap.add_argument("--compare", type=Path, help="another run folder to compare with (e.g. the rules baseline)")
    a = ap.parse_args()
    if a.run is None:
        runs = sorted((p for p in (ROOT / "data" / "pipeline_out").glob("*/predictions.jsonl")), key=lambda p: p.stat().st_mtime)
        if not runs:
            sys.exit("No run found. Run Code/run_pipeline.py first.")
        a.run = runs[-1].parent
    m = evaluate(a.run, a.gt)
    other = evaluate(a.compare, a.gt) if a.compare else None
    (a.run / "metrics.json").write_text(json.dumps(m, indent=2), encoding="utf-8")
    report = render(m, other)
    (a.run / "eval_report.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"\nWrote {a.run / 'metrics.json'} and {a.run / 'eval_report.md'}")


def evaluate(run: Path, gt_dir: Path) -> dict:
    preds = {json.loads(l)["doc_id"]: json.loads(l) for l in open(run / "predictions.jsonl", encoding="utf-8")}
    meta = json.loads((run / "run.json").read_text()) if (run / "run.json").exists() else {}
    gts = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (gt_dir / "gt").glob("*.json")}
    docs = [d for d in gts if d in preds]

    fstats = defaultdict(lambda: defaultdict(lambda: [0, 0]))      # cat -> field -> [correct, total]
    halluc = defaultdict(lambda: [0, 0])                            # field -> [pred non-null, gt-null labeled]
    li = defaultdict(lambda: [0, 0, 0, 0, 0])                       # cat -> [tp, n_pred, n_gt, count_match, invoices]
    doc_acc = defaultdict(lambda: [0, 0])
    times = defaultdict(list)
    per_page = {"digital": [], "ocr": [], "sheet": []}
    outcome = {"expected_success": 0, "succeeded": 0, "expected_fail": 0, "failed_cleanly": 0, "unexpected_failures": []}
    flags = defaultdict(lambda: [0, 0])                              # expected flag -> [detected, total]
    false_flags = [0, 0]
    missed_flags = []
    errors = []
    long_pdf = None
    cross = None

    # invoices that legitimately repeat an earlier document (e.g. a degraded scan of a PDF in the set)
    order = [r["doc_id"] for r in (json.loads(l) for l in open(run / "predictions.jsonl", encoding="utf-8"))]
    first_seen, implicit_dup = {}, set()
    for d in order:
        g = gts.get(d)
        if not g or g["expected_outcome"] == "fail" or g["category"] in ("long_pdf", "cross_verify"):
            continue
        for inv in g["invoices"]:
            if not inv.get("invoice_number"):
                continue
            key = (norm_id(inv["invoice_number"]), norm_text(inv.get("supplier_name") or inv.get("customer_name")))
            if key in first_seen and first_seen[key] != d:
                implicit_dup.add(d)
            first_seen.setdefault(key, d)

    for d in docs:
        g, p = gts[d], preds[d]
        cat = g["category"]
        t = p["timings_s"].get("total") or (p["timings_s"]["ingest"] + p["timings_s"]["extract_validate"])
        times[cat].append(t)
        if p["pages"]:
            per_page["ocr" if p["ocr_pages"] else "digital"].append(t / p["pages"])
        elif p["sheets"]:
            per_page["sheet"].append(t)
        if g["expected_outcome"] == "fail":
            outcome["expected_fail"] += 1
            outcome["failed_cleanly"] += p["status"] == "failed"
            continue
        outcome["expected_success"] += 1
        ok = p["status"] != "failed" and (p["invoices"] or g["expected_outcome"] == "no_invoice")
        outcome["succeeded"] += bool(ok)
        if p["status"] == "failed":
            outcome["unexpected_failures"].append(f"{d}: {p['error_code']} {p['error_message']}")

        # validation flags
        found_rules = {i["rule"] for inv in p["invoices"] for i in inv["issues"]}
        if d in implicit_dup and "duplicate" not in g.get("expected_flags", []):
            flags["duplicate (same invoice in another file of the set)"][1] += 1
            flags["duplicate (same invoice in another file of the set)"][0] += "DUPLICATE" in found_rules
        for fl in g.get("expected_flags", []):
            flags[fl][1] += 1
            hit_fl = bool(found_rules & FLAG_RULES.get(fl, set()))
            flags[fl][0] += hit_fl
            if not hit_fl:
                missed_flags.append(f"{d}: expected {fl}, rules found {sorted(found_rules) or 'nothing'}")
        if not g.get("expected_flags") and cat in FIELD_CATS and cat not in ("planted_error", "duplicate") and g["invoices"]:
            false_flags[1] += 1
            wrong = found_rules & MATH_RULES
            if d in implicit_dup:
                wrong -= {"DUPLICATE"}  # a true duplicate of another document in the set
            false_flags[0] += bool(wrong)

        if cat == "long_pdf":
            found = {norm_id(i["invoice"].get("invoice_number")) for i in p["invoices"]}
            exp = [norm_id(x["invoice_number"]) for x in g["invoices"]]
            pairs = pair_invoices(p["invoices"], g["invoices"])
            tot_ok = sum(1 for gi, pi in pairs if pi and field_correct("total", pi.get("total"), gi.get("total")))
            long_pdf = {"pages": p["pages"], "expected_invoices": len(exp), "found_invoices": len(p["invoices"]),
                        "invoice_numbers_found": sum(e in found for e in exp), "totals_correct": tot_ok,
                        "time_s": round(t, 2), "s_per_page": round(t / max(1, p["pages"]), 4)}
            continue
        if cat == "cross_verify":
            cv = p.get("cross_verification") or {}
            exp = g.get("expected_mismatches", [])
            got = cv.get("mismatches", [])
            hit = 0
            for e in exp:
                if e["field"] == "missing_in_ledger":
                    hit += e["invoice_number"] in cv.get("only_in_pdf", [])
                else:
                    key = "total" if e["field"] == "total" else e["field"].split(".")[-1]
                    hit += any(norm_id(x["invoice_number"]) == norm_id(e["invoice_number"]) and x["field"].endswith(key) for x in got)
            cv_missed = []
            for e in exp:
                if e["field"] == "missing_in_ledger":
                    ok_e = e["invoice_number"] in cv.get("only_in_pdf", [])
                else:
                    key = "total" if e["field"] == "total" else e["field"].split(".")[-1]
                    ok_e = any(norm_id(x["invoice_number"]) == norm_id(e["invoice_number"]) and x["field"].endswith(key) for x in got)
                if not ok_e:
                    cv_missed.append(f"{e['invoice_number']} {e['field']} (pdf {e.get('pdf_value')} vs ledger {e.get('ledger_value')})")
            exp_keys = {(norm_id(e["invoice_number"])) for e in exp}
            cross = {"expected_mismatches": len(exp), "detected": hit, "reported_mismatches": len(got),
                     "reported_on_clean_invoices": sum(norm_id(x["invoice_number"]) not in exp_keys for x in got),
                     "matched_invoices": cv.get("matched"), "only_in_pdf": cv.get("only_in_pdf"),
                     "only_in_ledger": cv.get("only_in_ledger"), "missed": cv_missed}
            continue
        if cat not in FIELD_CATS or not g["invoices"] or not g["labeled_fields"]:
            continue
        labeled = set(g["labeled_fields"])
        for gi, pi in pair_invoices(p["invoices"], g["invoices"]):
            all_ok = True
            for f in HEADER:
                if f not in labeled:
                    continue
                pv = pi.get(f) if pi else None
                ok_f = field_correct(f, pv, gi.get(f))
                fstats[cat][f][0] += ok_f
                fstats[cat][f][1] += 1
                all_ok &= ok_f
                if gi.get(f) is None:
                    halluc[f][1] += 1
                    halluc[f][0] += pv not in (None, "")
                elif not ok_f and len(errors) < 400:
                    errors.append({"doc_id": d, "category": cat, "field": f, "truth": gi.get(f), "predicted": pv})
            doc_acc[cat][0] += all_ok
            doc_acc[cat][1] += 1
            if "line_items" in labeled:
                tp, npred, ngt = line_item_scores((pi or {}).get("line_items", []), gi.get("line_items", []))
                s = li[cat]
                s[0] += tp; s[1] += npred; s[2] += ngt; s[3] += (npred == ngt); s[4] += 1

    def ratio(c, n):
        return round(c / n, 4) if n else None

    per_cat = {}
    for cat, fs in fstats.items():
        per_cat[cat] = {f: {"accuracy": ratio(*v), "n": v[1]} for f, v in fs.items()}
    overall = {}
    for f in HEADER:
        c = sum(fstats[cat][f][0] for cat in fstats)
        n = sum(fstats[cat][f][1] for cat in fstats)
        if n:
            overall[f] = {"accuracy": ratio(c, n), "n": n}
    all_c = sum(v[0] for fs in fstats.values() for v in fs.values())
    all_n = sum(v[1] for fs in fstats.values() for v in fs.values())
    lines = {cat: {"precision": ratio(s[0], s[1]), "recall": ratio(s[0], s[2]), "count_match": ratio(s[3], s[4]), "invoices": s[4]}
             for cat, s in li.items()}
    tot_li = [sum(s[i] for s in li.values()) for i in range(5)]
    methods = defaultdict(int)
    tokens = [0, 0]
    llm_s = 0.0
    for p in preds.values():
        for k, v in (p.get("extraction", {}).get("methods") or {}).items():
            methods[k] += v
        tokens[0] += p.get("extraction", {}).get("input_tokens") or 0
        tokens[1] += p.get("extraction", {}).get("output_tokens") or 0
        llm_s += p.get("extraction", {}).get("llm_s") or 0
    n_docs = len(docs)
    return {
        "run": str(run), "llm": meta.get("llm"), "created": meta.get("created"), "documents": n_docs,
        "field_accuracy_overall_micro": ratio(all_c, all_n), "fields_scored": all_n,
        "field_accuracy_by_field": overall, "field_accuracy_by_category": per_cat,
        "document_accuracy": {cat: {"accuracy": ratio(*v), "n": v[1]} for cat, v in doc_acc.items()},
        "hallucination": {f: {"rate": ratio(*v), "null_labels": v[1]} for f, v in halluc.items()},
        "hallucination_overall": ratio(sum(v[0] for v in halluc.values()), sum(v[1] for v in halluc.values())),
        "line_items": lines,
        "line_items_overall": {"precision": ratio(tot_li[0], tot_li[1]), "recall": ratio(tot_li[0], tot_li[2]),
                               "count_match": ratio(tot_li[3], tot_li[4]), "invoices": tot_li[4]},
        "success_rate": ratio(outcome["succeeded"], outcome["expected_success"]),
        "failure_rate": ratio(len(outcome["unexpected_failures"]), n_docs),
        "corrupted_rejected": f"{outcome['failed_cleanly']}/{outcome['expected_fail']}",
        "unexpected_failures": outcome["unexpected_failures"],
        "validation_detection": {fl: {"detected": v[0], "total": v[1], "recall": ratio(*v)} for fl, v in flags.items()},
        "false_flag_rate_clean_docs": {"flagged": false_flags[0], "clean_docs": false_flags[1], "rate": ratio(*false_flags)},
        "missed_validation_flags": missed_flags,
        "cross_verification": cross, "long_pdf": long_pdf,
        "time_s_per_doc": {cat: {"p50": round(pct(v, 50), 3), "p95": round(pct(v, 95), 3), "n": len(v)} for cat, v in times.items()},
        "time_s_per_page": {k: {"p50": round(pct(v, 50), 4), "p95": round(pct(v, 95), 4), "n": len(v)} for k, v in per_page.items() if v},
        "extraction_methods": dict(methods), "llm_tokens": {"input": tokens[0], "output": tokens[1]},
        "llm_time_s": round(llm_s, 1), "errors_sample": errors[:60],
    }


def fmt(x, pct_=True):
    if x is None:
        return "–"
    return f"{100 * x:.1f}%" if pct_ else f"{x}"


def render(m: dict, other: dict | None = None) -> str:
    L = [f"# Evaluation report", "", f"Run: `{m['run']}` · LLM: **{m['llm']}** · {m['created']} · {m['documents']} documents", ""]
    L += ["## Headline numbers", "", "| Metric | Value |" + (" Baseline |" if other else ""), "|---|---|" + ("---|" if other else "")]
    def row(name, key, f=fmt):
        v = m.get(key)
        o = other.get(key) if other else None
        L.append(f"| {name} | {f(v)} |" + (f" {f(o)} |" if other else ""))
    row("Field accuracy (all scored fields, micro)", "field_accuracy_overall_micro")
    row("Hallucination rate (value returned where the document has none)", "hallucination_overall")
    L.append(f"| Line items precision / recall | {fmt(m['line_items_overall']['precision'])} / {fmt(m['line_items_overall']['recall'])} |"
             + (f" {fmt(other['line_items_overall']['precision'])} / {fmt(other['line_items_overall']['recall'])} |" if other else ""))
    row("Document success rate", "success_rate")
    row("Unexpected failure rate", "failure_rate")
    L.append(f"| Corrupted files rejected cleanly | {m['corrupted_rejected']} |" + (f" {other['corrupted_rejected']} |" if other else ""))
    L.append(f"| Fields scored | {m['fields_scored']} |" + (f" {other['fields_scored']} |" if other else ""))
    L += ["", "## Field accuracy by field", "", "| Field | Accuracy | n |" + (" Baseline |" if other else ""), "|---|---|---|" + ("---|" if other else "")]
    for f, v in m["field_accuracy_by_field"].items():
        o = other["field_accuracy_by_field"].get(f, {}).get("accuracy") if other else None
        L.append(f"| {f} | {fmt(v['accuracy'])} | {v['n']} |" + (f" {fmt(o)} |" if other else ""))
    cats = sorted(m["field_accuracy_by_category"])
    L += ["", "## Field accuracy by document category", "", "| Field | " + " | ".join(cats) + " |", "|---|" + "---|" * len(cats)]
    for f in HEADER:
        L.append(f"| {f} | " + " | ".join(fmt(m["field_accuracy_by_category"][c].get(f, {}).get("accuracy")) for c in cats) + " |")
    L.append("| **all fields of a document correct** | " + " | ".join(fmt(m["document_accuracy"].get(c, {}).get("accuracy")) for c in cats) + " |")
    L += ["", "## Line items", "", "| Category | Precision | Recall | Item count correct | Invoices |", "|---|---|---|---|---|"]
    for c, v in sorted(m["line_items"].items()):
        L.append(f"| {c} | {fmt(v['precision'])} | {fmt(v['recall'])} | {fmt(v['count_match'])} | {v['invoices']} |")
    L += ["", "## Missing values (no invented data)", "",
          "Fields that are labeled **null** (not printed on the document, e.g. Northwind supplier/subtotal/tax). "
          "A non-null prediction here is a hallucination.", "", "| Field | Hallucination rate | Null labels |", "|---|---|---|"]
    for f, v in m["hallucination"].items():
        L.append(f"| {f} | {fmt(v['rate'])} | {v['null_labels']} |")
    L += ["", "## Validation", "", "| Planted problem | Detected | Recall |", "|---|---|---|"]
    for fl, v in m["validation_detection"].items():
        L.append(f"| {fl} | {v['detected']}/{v['total']} | {fmt(v['recall'])} |")
    ff = m["false_flag_rate_clean_docs"]
    L.append(f"| clean documents wrongly flagged (math/duplicate rules) | {ff['flagged']}/{ff['clean_docs']} | {fmt(ff['rate'])} |")
    if m.get("missed_validation_flags"):
        L += ["", "Missed planted problems:", ""] + [f"- {x}" for x in m["missed_validation_flags"]]
    cv = m.get("cross_verification")
    if cv:
        L += ["", "## PDF ↔ Excel cross-verification", "",
              f"Expected mismatches detected: **{cv['detected']}/{cv['expected_mismatches']}** · invoices matched: {cv['matched_invoices']} · "
              f"mismatches reported on invoices without a planted change: {cv['reported_on_clean_invoices']} · "
              f"only in PDFs: {cv['only_in_pdf']} · only in ledger: {cv['only_in_ledger']}"]
        if cv.get("missed"):
            L += ["", "Missed: " + "; ".join(cv["missed"])]
    lp = m.get("long_pdf")
    if lp:
        L += ["", "## Long PDF (merged Northwind invoices)", "",
              f"{lp['pages']} pages · invoices expected {lp['expected_invoices']}, found {lp['found_invoices']} · "
              f"invoice numbers recovered {lp['invoice_numbers_found']} · totals correct {lp['totals_correct']} · "
              f"total time {lp['time_s']} s ({lp['s_per_page']} s/page)"]
    L += ["", "## Processing time", "", "| Category | p50 s/doc | p95 s/doc | Docs |", "|---|---|---|---|"]
    for c, v in sorted(m["time_s_per_doc"].items()):
        L.append(f"| {c} | {v['p50']} | {v['p95']} | {v['n']} |")
    L += ["", "| Page type | p50 s/page | p95 s/page | Docs |", "|---|---|---|---|"]
    for c, v in m["time_s_per_page"].items():
        L.append(f"| {c} | {v['p50']} | {v['p95']} | {v['n']} |")
    L += ["", f"Extraction methods used: {m['extraction_methods']} · LLM tokens in/out: "
          f"{m['llm_tokens']['input']}/{m['llm_tokens']['output']} · total LLM time {m['llm_time_s']} s"]
    if m["unexpected_failures"]:
        L += ["", "Unexpected failures:", ""] + [f"- {x}" for x in m["unexpected_failures"]]
    L += ["", "## Sample of errors (for the failure analysis)", "", "| Document | Field | Truth | Predicted |", "|---|---|---|---|"]
    for e in m["errors_sample"][:30]:
        L.append(f"| {e['doc_id']} | {e['field']} | {str(e['truth'])[:40]} | {str(e['predicted'])[:40]} |")
    L += ["", "## Scoring rules", "", "invoice number: exact (ignoring spaces, '#', leading zeros) · date: same ISO date · "
          "names: fuzzy token-set similarity ≥ 85 · amounts: ±0.01 · currency: same ISO code · null labels: correct only if "
          "the prediction is null · line items: matched by amount (±0.01) and description similarity ≥ 70."]
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    main()
