"""Images (JPG/PNG/BMP/WEBP, multi-frame TIFF): clean up, then OCR."""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PIL import Image, ImageFilter, ImageOps, ImageSequence, UnidentifiedImageError

from .layout import mean_confidence, words_to_text
from .ocr import OcrBackend
from .types import CORRUPT_FILE, IngestError, PageText

Image.MAX_IMAGE_PIXELS = 200_000_000  # allow big scans, still refuse decompression bombs
LOW_CONF = 0.60
MIN_WIDTH = 1600   # upscale small images so characters are ~25-30 px tall for OCR


def load_frames(path: Path) -> list[Image.Image]:
    """All frames of an image file (1 for JPG/PNG, n for multi-page TIFF). Raises CORRUPT_FILE."""
    try:
        with Image.open(path) as probe:
            probe.verify()                       # catches truncated / broken files early
        frames = []
        with Image.open(path) as im:
            for fr in ImageSequence.Iterator(im):
                frames.append(ImageOps.exif_transpose(fr.copy()))
        if not frames:
            raise IngestError(CORRUPT_FILE, "Image contains no frames.")
        for fr in frames:
            fr.load()
        return frames
    except IngestError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as e:
        raise IngestError(CORRUPT_FILE, f"Cannot decode image: {e}") from e
    except Image.DecompressionBombError as e:
        raise IngestError(CORRUPT_FILE, f"Image too large: {e}") from e


def estimate_skew(gray: Image.Image, max_angle: float = 5.0, step: float = 0.5) -> float:
    """Projection-profile deskew: the angle that makes text rows sharpest."""
    small = gray.copy()
    small.thumbnail((900, 900))
    a = np.asarray(small, dtype=np.uint8)
    thr = a.mean() - 0.5 * a.std()
    ink = Image.fromarray(((a < thr) * 255).astype(np.uint8))
    best, best_score = 0.0, -1.0
    for ang in np.arange(-max_angle, max_angle + 1e-9, step):
        r = np.asarray(ink.rotate(float(ang), resample=Image.BILINEAR, fillcolor=0), dtype=np.float32)
        prof = r.sum(axis=1)
        score = float(np.var(prof))
        if score > best_score:
            best, best_score = float(ang), score
    return best


def preprocess(img: Image.Image) -> tuple[Image.Image, list[str]]:
    """Grayscale, upscale small images, deskew, gentle denoise and contrast stretch."""
    notes = []
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
        img = bg
    g = img.convert("L")
    if g.width < MIN_WIDTH:
        f = min(3.0, MIN_WIDTH / g.width)
        g = g.resize((int(g.width * f), int(g.height * f)), Image.LANCZOS)
        notes.append(f"upscaled x{f:.2f}")
    ang = estimate_skew(g)
    if abs(ang) >= 0.5:
        g = g.rotate(ang, resample=Image.BICUBIC, expand=True, fillcolor=255)
        notes.append(f"deskewed {ang:+.1f} deg")
    g = g.filter(ImageFilter.MedianFilter(3))
    g = ImageOps.autocontrast(g, cutoff=1)
    return g, notes


def ocr_image(img: Image.Image, backend: OcrBackend, page_no: int = 1) -> PageText:
    w0, h0 = img.size
    clean, notes = preprocess(img)
    words = backend.read(clean)
    conf = mean_confidence(words)
    warnings = []
    if not words:
        warnings.append("NO_TEXT_FOUND")
    elif conf is not None and conf < LOW_CONF:
        warnings.append(f"LOW_OCR_CONFIDENCE ({conf:.2f})")
    if notes:
        warnings.append("preprocess: " + ", ".join(notes))
    return PageText(page_no=page_no, text=words_to_text(words), method=f"ocr:{backend.name}",
                    ocr_confidence=conf, width=w0, height=h0, warnings=warnings)


def ingest_image(path: Path, backend: OcrBackend,
                 progress: Optional[Callable[[int, int], None]] = None) -> list[PageText]:
    frames = load_frames(path)
    pages = []
    for i, fr in enumerate(frames, 1):
        pages.append(ocr_image(fr, backend, page_no=i))
        if progress:
            progress(i, len(frames))
    return pages
