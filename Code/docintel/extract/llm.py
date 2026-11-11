"""Provider-agnostic LLM client (no SDK needed, plain HTTPS).

Configure with environment variables or a .env file in the project folder:

    DOCINTEL_LLM_PROVIDER = openai | anthropic | none
    DOCINTEL_LLM_MODEL    = e.g. gpt-4o-mini | claude-... | qwen2.5:7b-instruct
    DOCINTEL_LLM_API_KEY  = your key (not needed for a local server)
    DOCINTEL_LLM_BASE_URL = optional; for "openai" any OpenAI-compatible server works:
                            https://api.openai.com/v1           (OpenAI, default)
                            http://localhost:11434/v1           (Ollama, local GPU)
                            http://localhost:8000/v1            (vLLM, local GPU)
                            https://api.groq.com/openai/v1, https://openrouter.ai/api/v1, ...
                            https://generativelanguage.googleapis.com/v1beta/openai  (Gemini)
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


def load_dotenv(path: Optional[Path] = None) -> None:
    """Minimal .env loader (KEY=VALUE lines). Existing environment variables win."""
    candidates = [path] if path else [Path.cwd() / ".env", Path(__file__).resolve().parents[3] / ".env",
                                      Path(__file__).resolve().parents[2] / ".env"]
    for p in candidates:
        if p and p.is_file():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
            return


class LLMError(Exception):
    pass


@dataclass
class LLMResponse:
    text: str
    model: str
    latency_s: float
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None


class LLMClient:
    def __init__(self, provider: Optional[str] = None, model: Optional[str] = None, api_key: Optional[str] = None,
                 base_url: Optional[str] = None, timeout: float = 120, max_retries: int = 3, max_tokens: int = 2000):
        load_dotenv()
        self.provider = (provider or os.environ.get("DOCINTEL_LLM_PROVIDER") or "none").lower()
        self.model = model or os.environ.get("DOCINTEL_LLM_MODEL") or ""
        self.api_key = api_key or os.environ.get("DOCINTEL_LLM_API_KEY") or ""
        self.base_url = (base_url or os.environ.get("DOCINTEL_LLM_BASE_URL") or
                         ("https://api.anthropic.com/v1" if self.provider == "anthropic" else "https://api.openai.com/v1")).rstrip("/")
        self.timeout, self.max_retries, self.max_tokens = timeout, max_retries, max_tokens
        if self.provider not in ("openai", "anthropic", "none"):
            raise LLMError(f"Unknown provider '{self.provider}' (use openai, anthropic or none)")

    @property
    def enabled(self) -> bool:
        return self.provider != "none" and bool(self.model)

    @property
    def name(self) -> str:
        return f"{self.provider}:{self.model}" if self.enabled else "none"

    # ------------------------------------------------------------------ calls
    def complete_json(self, system: str, user: str) -> tuple[dict, LLMResponse]:
        if not self.enabled:
            raise LLMError("No LLM configured (set DOCINTEL_LLM_PROVIDER and DOCINTEL_LLM_MODEL).")
        last = None
        for attempt in range(self.max_retries):
            try:
                resp = self._call(system, user, json_mode=True)
                return parse_json_object(resp.text), resp
            except LLMError as e:
                last = e
                if "HTTP 4" in str(e) and "HTTP 429" not in str(e):
                    raise  # bad key / bad model name: retrying will not help
                if "Cannot reach" in str(e) and attempt >= 1:
                    raise  # server is down: fall back to the rules baseline without long waits
                time.sleep(2 ** attempt)
            except ValueError as e:  # model returned something that is not JSON: retry once more
                last = LLMError(f"Model did not return valid JSON: {e}")
        raise last

    def _call(self, system: str, user: str, json_mode: bool) -> LLMResponse:
        t0 = time.perf_counter()
        if self.provider == "anthropic":
            url = f"{self.base_url}/messages"
            body = {"model": self.model, "max_tokens": self.max_tokens, "temperature": 0,
                    "system": system, "messages": [{"role": "user", "content": user}]}
            headers = {"x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
            data = self._post(url, body, headers)
            text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            u = data.get("usage", {})
            return LLMResponse(text, data.get("model", self.model), time.perf_counter() - t0,
                               u.get("input_tokens"), u.get("output_tokens"))
        url = f"{self.base_url}/chat/completions"
        body = {"model": self.model, "temperature": 0, "max_tokens": self.max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        try:
            data = self._post(url, body, headers)
        except LLMError as e:
            if json_mode and "HTTP 400" in str(e) and "response_format" in str(e):
                return self._call(system, user, json_mode=False)  # server without JSON mode
            raise
        text = data["choices"][0]["message"].get("content") or ""
        u = data.get("usage") or {}
        return LLMResponse(text, data.get("model", self.model), time.perf_counter() - t0,
                           u.get("prompt_tokens"), u.get("completion_tokens"))

    def _post(self, url: str, body: dict, headers: dict) -> dict:
        req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:500]
            raise LLMError(f"HTTP {e.code} from {url}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise LLMError(f"Cannot reach {url}: {e}") from e


def parse_json_object(text: str) -> dict:
    """Parse the first JSON object in a model reply (tolerates ```json fences and prose around it)."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        v = json.loads(t)
        if isinstance(v, dict):
            return v
    except json.JSONDecodeError:
        pass
    start = t.find("{")
    if start < 0:
        raise ValueError("no JSON object in reply")
    depth, in_str, esc = 0, False, False
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(t[start:i + 1])
    raise ValueError("unterminated JSON object in reply")
