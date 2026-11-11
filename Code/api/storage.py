"""Safe storage of uploaded files: streamed to disk with a random name, size-limited, hashed."""
from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path

from fastapi import HTTPException, UploadFile

from . import config

ALLOWED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp", ".gif",
               ".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".txt"}


def safe_name(name: str) -> str:
    name = Path(name or "upload").name
    return re.sub(r"[^\w.\- ()]", "_", name)[:200] or "upload"


async def save_upload(f: UploadFile) -> tuple[Path, int, str]:
    """Returns (path, size, sha256). Rejects files over the limit or with an unsupported extension."""
    ext = Path(f.filename or "").suffix.lower()
    if ext not in ALLOWED_EXT:
        raise HTTPException(415, f"Unsupported file extension '{ext or 'none'}'. Allowed: {sorted(ALLOWED_EXT)}")
    limit = int(config.settings.max_upload_mb * 1024 * 1024)
    d = config.settings.storage_dir
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{uuid.uuid4().hex}{ext}"          # never use the client's file name on disk
    h, size = hashlib.sha256(), 0
    with open(path, "wb") as out:
        while True:
            chunk = await f.read(1 << 20)
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                out.close()
                path.unlink(missing_ok=True)
                raise HTTPException(413, f"'{f.filename}' is larger than {config.settings.max_upload_mb:g} MB")
            h.update(chunk)
            out.write(chunk)
    return path, size, h.hexdigest()
