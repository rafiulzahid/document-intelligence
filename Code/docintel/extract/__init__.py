"""Information extraction: ingested document -> list of ExtractedInvoice.

    from docintel.extract import extract_document, LLMClient
    invoices, info = extract_document(ingest_result, LLMClient())   # LLM from .env / environment

Order of preference per unit: deterministic table mapping (spreadsheets) -> LLM -> regex rules.
If the LLM is not configured or fails, the rules baseline is used and a warning is recorded.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

from ..ingest.types import IngestResult
from .grounding import ground
from .llm import LLMClient, LLMError
from .prompts import PROMPT_VERSION, SYSTEM_PROMPT, USER_TEMPLATE
from .rules import rules_extract
from .schema import ExtractedInvoice, FieldMeta, LineItem  # noqa: F401
from .segment import Unit, split_units

FILLABLE = ["invoice_number", "invoice_date", "subtotal", "tax", "total", "currency"]
MAX_CHARS = 14000  # per unit sent to the LLM (about 4k tokens)


def _extract_unit(unit: Unit, llm: Optional[LLMClient]) -> tuple[list[ExtractedInvoice], dict]:
    if unit.mapped:
        for inv in unit.mapped:
            inv.source = {**unit.source, **inv.source}
        return unit.mapped, {"method": "table_mapping", "llm_s": 0.0}
    text = unit.text
    note = None
    if len(text) > MAX_CHARS:
        text, note = text[:MAX_CHARS], f"text truncated to {MAX_CHARS} characters for the LLM"
    info = {"method": "rules", "llm_s": 0.0}
    raw, method = None, "rules"
    if llm is not None and llm.enabled:
        try:
            raw, resp = llm.complete_json(SYSTEM_PROMPT, USER_TEMPLATE.format(text=text))
            method = f"llm:{llm.model}"
            info.update(method=method, llm_s=round(resp.latency_s, 3), input_tokens=resp.input_tokens,
                        output_tokens=resp.output_tokens)
        except LLMError as e:
            info["llm_error"] = str(e)[:300]
    if raw is None:
        raw = rules_extract(unit.text)
    inv = ground(raw, unit.text, method)
    if method != "rules":   # fill fields the LLM left empty (or that failed grounding) from the rules baseline
        backup = ground(rules_extract(unit.text), unit.text, "rules")
        for f in FILLABLE:
            if getattr(inv, f) is None and getattr(backup, f) is not None:
                setattr(inv, f, getattr(backup, f))
                m = backup.field_meta[f]
                m.status, m.note = "uncertain", "LLM returned no usable value; filled by the rules baseline"
                inv.field_meta[f] = m
        if not inv.line_items and backup.line_items:
            inv.line_items = backup.line_items
    inv.source = dict(unit.source)
    if note:
        inv.warnings.append(note)
    if "llm_error" in info:
        inv.warnings.append(f"LLM failed, used rules baseline: {info['llm_error'][:120]}")
    return [inv], info


def extract_document(res: IngestResult, llm: Optional[LLMClient] = None,
                     workers: int = 4) -> tuple[list[ExtractedInvoice], dict]:
    """Returns (invoices, info). Never raises for content problems."""
    t0 = time.perf_counter()
    if not res.ok:
        return [], {"units": 0, "error": res.error_code, "extract_s": 0.0}
    units = split_units(res)
    results: list[tuple[list[ExtractedInvoice], dict]] = []
    if len(units) > 1 and llm is not None and llm.enabled and workers > 1:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda u: _extract_unit(u, llm), units))
    else:
        results = [_extract_unit(u, llm) for u in units]
    invoices = [inv for invs, _ in results for inv in invs]
    methods = {}
    for _, i in results:
        methods[i["method"]] = methods.get(i["method"], 0) + 1
    info = {"units": len(units), "methods": methods, "prompt_version": PROMPT_VERSION,
            "llm": llm.name if llm else "none",
            "llm_s": round(sum(i.get("llm_s", 0.0) for _, i in results), 3),
            "llm_errors": [i["llm_error"] for _, i in results if "llm_error" in i][:5],
            "input_tokens": sum(i.get("input_tokens") or 0 for _, i in results),
            "output_tokens": sum(i.get("output_tokens") or 0 for _, i in results),
            "extract_s": round(time.perf_counter() - t0, 3)}
    return invoices, info
