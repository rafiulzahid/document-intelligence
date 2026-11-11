"""OCR backends behind one interface.

    backend = get_backend("auto")   # EasyOCR on a CUDA GPU if installed, otherwise Tesseract
    words = backend.read(pil_image)  # -> list of (x0, y0, x1, y1, text, confidence 0-1)

Choose explicitly with get_backend("tesseract") / get_backend("easyocr"), or set the
environment variable DOCINTEL_OCR=tesseract|easyocr.
"""
from __future__ import annotations

import os
import shutil
import threading
from typing import Optional

import numpy as np
from PIL import Image

from .layout import Word
from .types import OCR_UNAVAILABLE, IngestError

_lock = threading.Lock()
_cache: dict[str, "OcrBackend"] = {}


class OcrBackend:
    name = "base"
    parallel_safe = True  # can several threads call read() at once?

    def read(self, img: Image.Image) -> list[Word]:
        raise NotImplementedError


class TesseractBackend(OcrBackend):
    name = "tesseract"
    parallel_safe = True  # each call runs its own tesseract process

    def __init__(self, lang: str = "eng"):
        import pytesseract
        if not shutil.which("tesseract") and not os.environ.get("TESSERACT_CMD"):
            raise IngestError(OCR_UNAVAILABLE, "Tesseract is not installed (sudo apt install tesseract-ocr).")
        if os.environ.get("TESSERACT_CMD"):
            pytesseract.pytesseract.tesseract_cmd = os.environ["TESSERACT_CMD"]
        self.pt = pytesseract
        self.lang = lang

    def read(self, img: Image.Image) -> list[Word]:
        d = self.pt.image_to_data(img, lang=self.lang, config="--oem 1 --psm 3",
                                  output_type=self.pt.Output.DICT)
        words = []
        for i, t in enumerate(d["text"]):
            if not str(t).strip():
                continue
            conf = float(d["conf"][i])
            x, y, w, h = d["left"][i], d["top"][i], d["width"][i], d["height"][i]
            words.append((float(x), float(y), float(x + w), float(y + h), t, conf / 100 if conf >= 0 else None))
        return words


class EasyOcrBackend(OcrBackend):
    name = "easyocr"
    parallel_safe = False  # one model on the GPU; calls are serialised

    def __init__(self, langs=("en",), gpu: Optional[bool] = None):
        try:
            import easyocr
            import torch
        except ImportError as e:
            raise IngestError(OCR_UNAVAILABLE, "EasyOCR is not installed (pip install easyocr).") from e
        use_gpu = torch.cuda.is_available() if gpu is None else gpu
        self.reader = easyocr.Reader(list(langs), gpu=use_gpu, verbose=False)
        self.gpu = use_gpu

    def read(self, img: Image.Image) -> list[Word]:
        arr = np.array(img.convert("L"))
        with _lock:
            res = self.reader.readtext(arr, detail=1, paragraph=False)
        words = []
        for box, text, conf in res:
            xs = [p[0] for p in box]; ys = [p[1] for p in box]
            words.append((float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys)), text, float(conf)))
        return words


def get_backend(name: str = "auto") -> OcrBackend:
    name = (os.environ.get("DOCINTEL_OCR") or name or "auto").lower()
    with _lock:
        if name in _cache:
            return _cache[name]
    if name == "auto":
        backend = None
        try:
            import torch  # noqa: F401
            import easyocr  # noqa: F401
            if torch.cuda.is_available():
                backend = EasyOcrBackend()
        except Exception:  # noqa: BLE001
            backend = None
        backend = backend or TesseractBackend()
    elif name == "tesseract":
        backend = TesseractBackend()
    elif name == "easyocr":
        backend = EasyOcrBackend()
    else:
        raise IngestError(OCR_UNAVAILABLE, f"Unknown OCR backend '{name}'.")
    with _lock:
        _cache[name] = backend
    return backend
