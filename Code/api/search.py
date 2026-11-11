"""Keyword, semantic and hybrid search over stored documents, with structured filters.

keyword   PostgreSQL full-text search (to_tsvector/websearch_to_tsquery, GIN index) plus fuzzy
          matching of invoice numbers and party names; on SQLite a simple term match.
semantic  pgvector cosine search (HNSW index) when the extension is available, otherwise the same
          cosine similarity computed in Python over the stored vectors.
hybrid    reciprocal rank fusion of the two rankings.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from sqlalchemy import and_, or_, select, text

from . import config, db, embeddings
from .models import Chunk, Document, Invoice

RRF_K = 60


def _filters(q, f: dict):
    conds = []
    if f.get("supplier"):
        conds.append(Invoice.supplier_name.ilike(f"%{f['supplier']}%"))
    if f.get("customer"):
        conds.append(Invoice.customer_name.ilike(f"%{f['customer']}%"))
    if f.get("invoice_number"):
        conds.append(Invoice.invoice_number.ilike(f"%{f['invoice_number']}%"))
    if f.get("date_from"):
        conds.append(Invoice.invoice_date >= f["date_from"])
    if f.get("date_to"):
        conds.append(Invoice.invoice_date <= f["date_to"])
    if f.get("min_total") is not None:
        conds.append(Invoice.total >= f["min_total"])
    if f.get("max_total") is not None:
        conds.append(Invoice.total <= f["max_total"])
    if f.get("currency"):
        conds.append(Invoice.currency == f["currency"].upper())
    if f.get("status"):
        conds.append(Invoice.status == f["status"])
    if f.get("file_type"):
        conds.append(Document.detected_type == f["file_type"])
    return q.where(and_(*conds)) if conds else q


def _terms(q: str) -> list[str]:
    return [t for t in re.findall(r"\w+", q.lower()) if len(t) > 1]


def keyword_ranking(s, q: str, limit: int = 200) -> list[tuple[int, float]]:
    """[(chunk_id, score)] best first."""
    out: dict[int, float] = {}
    if config.settings.is_postgres:
        rows = s.execute(text(
            "SELECT id, ts_rank(to_tsvector('simple', text), websearch_to_tsquery('simple', :q)) AS r FROM chunks "
            "WHERE to_tsvector('simple', text) @@ websearch_to_tsquery('simple', :q) ORDER BY r DESC LIMIT :n"),
            {"q": q, "n": limit}).all()
        out.update({r[0]: float(r[1]) for r in rows})
        like = f"%{q}%"
        fuzzy = "OR similarity(i.supplier_name, :q) > 0.35 OR similarity(i.customer_name, :q) > 0.35" if db.state["pg_trgm"] else ""
        rows = s.execute(text(
            "SELECT c.id FROM chunks c JOIN invoices i ON c.invoice_id = i.id WHERE c.kind = 'invoice' AND "
            f"(i.invoice_number ILIKE :like OR i.supplier_name ILIKE :like OR i.customer_name ILIKE :like {fuzzy}) LIMIT :n"),
            {"q": q, "like": like, "n": limit}).all()
        for (cid,) in rows:
            out[cid] = out.get(cid, 0.0) + 1.0
    else:
        terms = _terms(q)
        if not terms:
            return []
        rows = s.execute(select(Chunk.id, Chunk.text).where(or_(*[Chunk.text.ilike(f"%{t}%") for t in terms]))).all()
        for cid, txt in rows:
            low = txt.lower()
            hits = sum(1 for t in terms if t in low)
            out[cid] = hits / len(terms) + 0.01 * sum(low.count(t) for t in terms)
    return sorted(out.items(), key=lambda x: -x[1])[:limit]


def semantic_ranking(s, q: str, limit: int = 200) -> list[tuple[int, float]]:
    tag = embeddings.model_tag()
    if not tag:
        return []
    vec = embeddings.embed([q])[0]
    if db.state["pgvector"] and db.state["vector_dim"] == len(vec):
        rows = s.execute(text("SELECT chunk_id, 1 - (embedding <=> CAST(:v AS vector)) FROM chunk_embeddings "
                              "ORDER BY embedding <=> CAST(:v AS vector) LIMIT :n"), {"v": json.dumps(vec), "n": limit}).all()
        return [(r[0], float(r[1])) for r in rows]
    scored = []
    for cid, emb in s.execute(select(Chunk.id, Chunk.embedding).where(Chunk.embedding.is_not(None))).all():
        if isinstance(emb, dict) and emb.get("m") == tag:
            scored.append((cid, embeddings.cosine(vec, emb["v"])))
    return sorted(scored, key=lambda x: -x[1])[:limit]


def rrf(*rankings) -> list[tuple[int, float]]:
    score: dict[int, float] = {}
    for r in rankings:
        for rank, (cid, _) in enumerate(r):
            score[cid] = score.get(cid, 0.0) + 1.0 / (RRF_K + rank + 1)
    return sorted(score.items(), key=lambda x: -x[1])


def snippet(txt: str, q: str, width: int = 220) -> str:
    low = txt.lower()
    pos = min((low.find(t) for t in _terms(q) if t in low), default=0)
    start = max(0, pos - width // 3)
    s = re.sub(r"\s+", " ", txt[start:start + width]).strip()
    return ("…" if start > 0 else "") + s + ("…" if start + width < len(txt) else "")


def search(s, q: Optional[str], mode: str = "hybrid", filters: Optional[dict] = None, limit: int = 20) -> dict:
    from .serializers import invoice_brief
    filters = {k: v for k, v in (filters or {}).items() if v not in (None, "")}
    base = select(Invoice).join(Document, Invoice.document_id == Document.id)
    base = _filters(base, filters)
    if not q or not q.strip():
        invs = s.execute(base.order_by(Invoice.invoice_date.desc().nulls_last(), Invoice.id.desc()).limit(limit)).scalars().all()
        return {"query": q, "mode": "filter", "count": len(invs),
                "results": [{"score": None, "snippet": None, "match": "filters", **invoice_brief(i)} for i in invs]}
    if mode == "keyword":
        ranking, used = keyword_ranking(s, q), "keyword"
    elif mode == "semantic":
        ranking, used = semantic_ranking(s, q), "semantic"
        if not embeddings.model_tag():
            used = "semantic (disabled: EMBED_PROVIDER=none)"
    else:
        ranking, used = rrf(keyword_ranking(s, q), semantic_ranking(s, q)), "hybrid"
    if not ranking:
        return {"query": q, "mode": used, "count": 0, "results": []}
    allowed = None
    if filters:
        allowed = {i.id for i in s.execute(base).scalars().all()}
    ids = [cid for cid, _ in ranking]
    chunks = {c.id: c for c in s.execute(select(Chunk).where(Chunk.id.in_(ids))).scalars().all()}
    results, seen = [], set()
    for cid, sc in ranking:
        c = chunks.get(cid)
        if c is None:
            continue
        doc = s.get(Document, c.document_id)
        inv = s.get(Invoice, c.invoice_id) if c.invoice_id else None
        if inv is None and doc.invoices:
            inv = next((i for i in doc.invoices if c.page_no in ((i.source or {}).get("pages") or [])), doc.invoices[0])
        if allowed is not None and (inv is None or inv.id not in allowed):
            continue
        key = ("inv", inv.id) if inv else ("doc", doc.id)
        if key in seen:
            continue
        seen.add(key)
        item = invoice_brief(inv) if inv else {"invoice_id": None, "document_id": doc.id, "filename": doc.filename,
                                                "document_status": doc.status}
        results.append({"score": round(sc, 4), "match": c.kind, "page": c.page_no, "snippet": snippet(c.text, q), **item})
        if len(results) >= limit:
            break
    return {"query": q, "mode": used, "count": len(results), "results": results}
