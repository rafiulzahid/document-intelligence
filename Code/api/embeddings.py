"""Text embeddings for semantic search.

EMBED_PROVIDER=openai                 any OpenAI-compatible /embeddings endpoint, e.g. Ollama:
                                      `ollama pull nomic-embed-text`, EMBED_MODEL=nomic-embed-text
EMBED_PROVIDER=sentence_transformers  local model (pip install sentence-transformers), EMBED_MODEL=all-MiniLM-L6-v2
EMBED_PROVIDER=hash                   no model: hashed character trigrams (fuzzy lexical, robust to OCR noise)
EMBED_PROVIDER=none                   semantic search disabled

Every stored vector is tagged with the model that made it, so a query is only compared with
vectors from the same model.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import urllib.request
from typing import Optional

from . import config

HASH_DIM = 384
_st_model = None
_lock = threading.Lock()


def model_tag() -> Optional[str]:
    p = config.settings.embed_provider
    if p == "none":
        return None
    if p == "hash":
        return f"hash-{HASH_DIM}"
    return f"{p}:{config.settings.embed_model}"


def _hash_embed(text: str) -> list[float]:
    v = [0.0] * HASH_DIM
    t = " " + re.sub(r"\s+", " ", text.lower()) + " "
    for i in range(len(t) - 2):
        h = int.from_bytes(hashlib.md5(t[i:i + 3].encode()).digest()[:4], "little")
        v[h % HASH_DIM] += 1.0 if (h >> 31) & 1 else -1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [round(x / n, 5) for x in v]


def embed(texts: list[str]) -> Optional[list[list[float]]]:
    """Embeds texts with the configured provider; None if semantic search is disabled or unavailable."""
    p = config.settings.embed_provider
    texts = [t[:4000] for t in texts]
    if not texts or p == "none":
        return None
    if p == "hash":
        return [_hash_embed(t) for t in texts]
    if p == "sentence_transformers":
        global _st_model
        with _lock:
            if _st_model is None:
                from sentence_transformers import SentenceTransformer
                _st_model = SentenceTransformer(config.settings.embed_model)
        return [list(map(float, v)) for v in _st_model.encode(texts, normalize_embeddings=True)]
    if p == "openai":
        url = config.settings.embed_base_url.rstrip("/") + "/embeddings"
        headers = {"content-type": "application/json"}
        if config.settings.embed_api_key:
            headers["authorization"] = f"Bearer {config.settings.embed_api_key}"
        out = []
        for i in range(0, len(texts), 32):
            body = json.dumps({"model": config.settings.embed_model, "input": texts[i:i + 32]}).encode()
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.load(r)["data"]
            out += [normalize(d["embedding"]) for d in sorted(data, key=lambda d: d["index"])]
        return out
    raise ValueError(f"unknown EMBED_PROVIDER '{p}'")


def normalize(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))
