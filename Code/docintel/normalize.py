"""Normalisation of raw extracted strings: amounts, dates, currencies, names.

Values are normalised in Python after the LLM call (never inside the prompt), so the model only
copies what is written and every conversion is deterministic and testable.
"""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from typing import Optional

# ---------------------------------------------------------------- amounts
_NUM = re.compile(r"-?\d[\d\s.,']*\d|-?\d")


def parse_amount(value) -> Optional[float]:
    """'$ 1 234,56' -> 1234.56 · '$1,444.80' -> 1444.8 · 'RM 7.60' -> 7.6 · '(12.50)' -> -12.5."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    s = str(value).strip()
    neg = s.startswith("(") and s.endswith(")") or s.startswith("-") or s.endswith("-")
    s = re.sub(r"[^\d,.']", "", s.replace(" ", ""))
    s = s.replace("'", "")
    if not re.search(r"\d", s):
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".") if re.search(r",\d{1,2}$", s) and s.count(",") == 1 else s.replace(",", "")
    elif s.count(".") > 1:
        head, _, tail = s.rpartition(".")
        s = head.replace(".", "") + ("." + tail if len(tail) <= 2 else tail)
    try:
        v = round(float(s), 2)
    except ValueError:
        return None
    return -v if neg else v


def numbers_in_text(text: str) -> set[float]:
    """Every number written in a text, parsed with the same rules (used for grounding)."""
    out = set()
    for m in _NUM.finditer(text or ""):
        tok = m.group(0)
        for cand in {tok, tok.replace(" ", "")} | set(tok.split()):
            v = parse_amount(cand)
            if v is not None:
                out.add(abs(v))
    return out


# ---------------------------------------------------------------- dates
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def parse_date(value, prefer_dayfirst: Optional[bool] = None) -> tuple[Optional[str], Optional[str]]:
    """Return (ISO date, note). note is set when the order was ambiguous (e.g. 03/04/2021)."""
    if value is None:
        return None, None
    if isinstance(value, (datetime, date)):
        return (value.date() if isinstance(value, datetime) else value).isoformat(), None
    s = re.sub(r"\s+", " ", str(value).strip().strip("()").replace(",", " ")).strip()
    if not s:
        return None, None
    # ISO / year first
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[ T].*)?", s)
    if m:
        return _mk(int(m[1]), int(m[2]), int(m[3])), None
    # numeric d/m/y or m/d/y
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})(?: .*)?", s)
    if m:
        a, b, y = int(m[1]), int(m[2]), _year(m[3])
        if a > 12 and b <= 12:
            return _mk(y, b, a), None
        if b > 12 and a <= 12:
            return _mk(y, a, b), None
        if a == b:
            return _mk(y, a, b), None
        dayfirst = True if prefer_dayfirst is None else prefer_dayfirst
        iso = _mk(y, b, a) if dayfirst else _mk(y, a, b)
        return iso, f"ambiguous day/month order in '{s}', read as {'DD/MM' if dayfirst else 'MM/DD'}"
    # textual months: 22 MAR 2018, 22-Mar-2018, Mar 22 2018, March 22, 2018
    m = re.fullmatch(r"(\d{1,2})[ -/]?([A-Za-z]{3,9})\.?[ -/]?(\d{2,4})", s)
    if m and m[2][:3].lower() in MONTHS:
        return _mk(_year(m[3]), MONTHS[m[2][:3].lower()], int(m[1])), None
    m = re.fullmatch(r"([A-Za-z]{3,9})\.? (\d{1,2})(?:st|nd|rd|th)? (\d{2,4})", s)
    if m and m[1][:3].lower() in MONTHS:
        return _mk(_year(m[3]), MONTHS[m[1][:3].lower()], int(m[2])), None
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s)
    if m:
        return _mk(int(m[1]), int(m[2]), int(m[3])), None
    return None, f"unrecognised date '{s}'"


def _year(y: str) -> int:
    y = int(y)
    return y + 2000 if y < 70 else (y + 1900 if y < 100 else y)


def _mk(y, mo, d) -> Optional[str]:
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------- currency
CURRENCY_SYMBOLS = {"$": "USD", "US$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR", "৳": "BDT",
                    "RM": "MYR", "Tk": "BDT", "TK": "BDT", "Rs": "INR", "S$": "SGD", "A$": "AUD", "C$": "CAD",
                    "CHF": "CHF", "₩": "KRW", "₺": "TRY", "zł": "PLN", "R$": "BRL"}
ISO_CODES = {"USD", "EUR", "GBP", "JPY", "INR", "BDT", "MYR", "SGD", "AUD", "CAD", "CHF", "CNY", "KRW",
             "TRY", "PLN", "BRL", "SEK", "NOK", "DKK", "ZAR", "AED", "SAR", "HKD", "NZD", "MXN", "IDR", "THB"}


def normalize_currency(value) -> tuple[Optional[str], Optional[str]]:
    if value is None:
        return None, None
    s = str(value).strip()
    if not s:
        return None, None
    up = s.upper()
    if up in ISO_CODES:
        return up, None
    for code in ISO_CODES:
        if re.search(rf"\b{code}\b", up):
            return code, None
    for sym in sorted(CURRENCY_SYMBOLS, key=len, reverse=True):
        if sym in s:
            note = "'$' read as USD (symbol is shared by several currencies)" if CURRENCY_SYMBOLS[sym] == "USD" else None
            return CURRENCY_SYMBOLS[sym], note
    return None, f"unrecognised currency '{s}'"


# ---------------------------------------------------------------- text
def norm_text(s) -> str:
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).casefold()
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_id(s) -> str:
    if s is None:
        return ""
    return re.sub(r"[\s#:]", "", str(s)).upper().lstrip("0") or "0"
