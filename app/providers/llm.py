"""Pluggable LLM client: Groq (default, free) / OpenAI / Anthropic / Gemini. Always returns parsed JSON or raises."""
from __future__ import annotations

import json
import logging
import re
import time

import httpx

from app.config import Settings, get_settings
from app.providers.base import CircuitBreaker, ProviderError

log = logging.getLogger("unclone.llm")

DEFAULT_MODELS = {
    # Benchmarked on our intent prompt (Oct 2026): qwen3.8-27b fastest (~0.75 s) and the only one that understood
    # garbled Kannada transcripts. llama-3.3-70b-versatile has been retired by Groq.
    "groq": "qwen/qwen3.8-27b",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-5",
    "gemini": "gemini-2.5-flash",
}


def _extract_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                pass
    raise ProviderError(f"LLM did not return JSON: {text[:200]}")


# If Groq retires a model (HTTP 404/400 'model_not_found' / 'decommissioned'), try these next.
GROQ_FALLBACKS = ["qwen/qwen3.8-27b", "openai/gpt-oss-120b", "openai/gpt-oss-20b"]


def _reasoning_opts(model: str) -> dict:
    m = model.lower()
    if "gpt-oss" in m:
        return {"reasoning_effort": "low"}
    if "qwen3" in m:
        return {"reasoning_effort": "none"}
    return {}


class LLM:
    def __init__(self, s: Settings | None = None):
        self.s = s or get_settings()
        self.breaker = CircuitBreaker("llm", self.s.breaker_fail_threshold, self.s.breaker_cooldown_s)
        self._http: httpx.AsyncClient | None = None

    @property
    def provider(self) -> str:
        return self.s.llm_provider

    _model_override: str = ""

    @property
    def model(self) -> str:
        return self._model_override or self.s.llm_model or DEFAULT_MODELS.get(self.provider, "")

    @property
    def available(self) -> bool:
        key = {"groq": self.s.effective_groq_key, "openai": self.s.openai_api_key, "anthropic": self.s.anthropic_api_key, "gemini": self.s.gemini_api_key}.get(self.provider, "")
        return self.provider != "none" and bool(key) and not self.breaker.is_open

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(self.s.llm_timeout_s, connect=4.0))
        return self._http

    async def json_chat(self, system: str, user: str) -> dict:
        if not self.available:
            raise ProviderError(f"LLM provider '{self.provider}' unavailable (no key or circuit open)")
        t0 = time.perf_counter()
        try:
            out = await self._dispatch(system, user)
            self.breaker.ok()
            log.debug("llm %s %.0f ms", self.provider, (time.perf_counter() - t0) * 1000)
            return out
        except Exception as e:
            self.breaker.fail(e)
            raise ProviderError(f"LLM {self.provider} failed: {type(e).__name__}: {e}"[:300]) from e

    async def _dispatch(self, system: str, user: str) -> dict:
        p, c = self.provider, self._client()
        if p in ("groq", "openai"):
            url = "https://api.groq.com/openai/v1/chat/completions" if p == "groq" else "https://api.openai.com/v1/chat/completions"
            keys = self.s.groq_keys if p == "groq" else [self.s.openai_api_key]
            models = [self.model] + ([m for m in GROQ_FALLBACKS if m != self.model] if p == "groq" else [])
            last: Exception | None = None
            for key in (keys or [""]):
                for model in models:
                    body = {
                        "model": model,
                        "temperature": 0,
                        "max_tokens": 1200,
                        "response_format": {"type": "json_object"},
                        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    }
                    if p == "groq":
                        body.update(_reasoning_opts(model))
                    r = await c.post(url, headers={"Authorization": f"Bearer {key}"}, json=body)
                    if r.status_code == 429 and len(keys) > 1:
                        log.warning("Groq key rate-limited (429); failing over to next key")
                        break
                    if r.status_code in (400, 404) and ("model" in r.text.lower() and ("not" in r.text.lower() or "decommission" in r.text.lower())):
                        log.warning("LLM model %s unavailable (%s); trying next", model, r.status_code)
                        last = ProviderError(f"model {model} unavailable")
                        continue
                    r.raise_for_status()
                    if model != self.model:
                        self._model_override = model  # remember the working one
                    return _extract_json(r.json()["choices"][0]["message"]["content"])
            raise last or ProviderError("no LLM model available")
        if p == "anthropic":
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": self.s.anthropic_api_key, "anthropic-version": "2023-06-01"},
                json={
                    "model": self.model,
                    "max_tokens": 600,
                    "temperature": 0,
                    "system": system + "\nRespond with a single JSON object only.",
                    "messages": [{"role": "user", "content": user}],
                },
            )
            r.raise_for_status()
            parts = r.json().get("content", [])
            return _extract_json("".join(b.get("text", "") for b in parts if b.get("type") == "text"))
        if p == "gemini":
            r = await c.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
                headers={"x-goog-api-key": self.s.gemini_api_key},
                json={
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}],
                    "generationConfig": {"temperature": 0, "responseMimeType": "application/json", "maxOutputTokens": 600},
                },
            )
            r.raise_for_status()
            cands = r.json().get("candidates") or []
            if not cands:
                raise ProviderError("gemini returned no candidates")
            return _extract_json("".join(pt.get("text", "") for pt in cands[0].get("content", {}).get("parts", [])))
        raise ProviderError(f"unknown LLM provider {p}")

    async def close(self):
        if self._http:
            await self._http.aclose()


_llm: LLM | None = None


def get_llm() -> LLM:
    global _llm
    if _llm is None:
        _llm = LLM()
    return _llm
