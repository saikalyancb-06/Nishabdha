"""Text-to-speech via edge-tts (free Microsoft neural voices: en-IN / hi-IN / kn-IN).

If edge-tts fails (no internet / Microsoft changes its endpoint - old edge-tts versions get HTTP 403,
keep it upgraded) the reply is sent as text with `client_tts=True` so the frontend speaks it with the
browser's speechSynthesis. The call never goes silent.
"""
from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from dataclasses import dataclass

from app.config import Settings, get_settings
from app.providers.base import CircuitBreaker

log = logging.getLogger("unclone.tts")


import base64
import httpx

@dataclass
class Speech:
    text: str
    lang: str
    audio_mp3: bytes | None
    voice: str
    source: str  # sarvam | edge | client

    @property
    def client_tts(self) -> bool:
        return self.audio_mp3 is None


class TTS:
    def __init__(self, s: Settings | None = None, cache_size: int = 256):
        self.s = s or get_settings()
        self._cache: OrderedDict[tuple[str, str], bytes] = OrderedDict()
        self._cache_size = cache_size
        self.sarvam_breaker = CircuitBreaker("sarvam_tts", 2, 60.0)
        self.edge_breaker = CircuitBreaker("edge_tts", 2, 60.0)
        self.breaker = self.sarvam_breaker if self.s.tts_provider == "sarvam" else self.edge_breaker
        self._http: httpx.AsyncClient | None = None

    async def _sarvam(self, text: str, lang: str) -> bytes:
        key = self.s.effective_sarvam_key
        if not key:
            raise RuntimeError("SARVAM_AI_API / SARVAM_API_KEY not set")
        sarvam_lang = {"hi": "hi-IN", "kn": "kn-IN"}.get(lang, "en-IN")
        payload = {
            "text": text,
            "target_language_code": sarvam_lang,
            "speaker": self.s.sarvam_tts_speaker,
            "model": self.s.sarvam_tts_model,
            "speech_sample_rate": 16000,
        }
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(self.s.tts_timeout_s, connect=4.0))
        r = await self._http.post(
            "https://api.sarvam.ai/text-to-speech",
            headers={"api-subscription-key": key, "Content-Type": "application/json"},
            json=payload,
        )
        if r.status_code >= 400:
            raise RuntimeError(f"sarvam tts HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
        audios = data.get("audios") or []
        if not audios:
            raise RuntimeError("sarvam tts returned no audio")
        return base64.b64decode(audios[0])

    async def _edge(self, text: str, voice: str) -> bytes:
        import edge_tts

        buf = bytearray()
        async for chunk in edge_tts.Communicate(text, voice).stream():
            if chunk.get("type") == "audio":
                buf.extend(chunk["data"])
        if not buf:
            raise RuntimeError("edge-tts returned no audio")
        return bytes(buf)

    async def speak(self, text: str, lang: str = "en") -> Speech:
        voice = self.s.voice_for(lang)
        source = self.s.tts_provider
        vname = f"sarvam-{self.s.sarvam_tts_speaker}" if source == "sarvam" else voice
        key = (vname, text)
        if key in self._cache:
            self._cache.move_to_end(key)
            return Speech(text, lang, self._cache[key], vname, source)
        if self.s.tts_provider == "none" or not text.strip():
            return Speech(text, lang, None, vname, "client")

        # 1. Try Sarvam TTS if configured
        if source == "sarvam" and not self.sarvam_breaker.is_open:
            try:
                audio = await asyncio.wait_for(self._sarvam(text, lang), self.s.tts_timeout_s)
                self.sarvam_breaker.ok()
                self._cache[key] = audio
                if len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
                return Speech(text, lang, audio, vname, "sarvam")
            except Exception as e:
                self.sarvam_breaker.fail(e)
                log.warning("Sarvam TTS failed, falling back to edge-tts: %s", e)

        # 2. Try Edge TTS as primary or fallback
        if not self.edge_breaker.is_open:
            try:
                audio = await asyncio.wait_for(self._edge(text, voice), self.s.tts_timeout_s)
                self.edge_breaker.ok()
                self._cache[(voice, text)] = audio
                if len(self._cache) > self._cache_size:
                    self._cache.popitem(last=False)
                return Speech(text, lang, audio, voice, "edge")
            except Exception as e:
                self.edge_breaker.fail(e)
                log.warning("Edge TTS failed, falling back to client speech: %s", e)

        # 3. Fall back to client speech synthesis
        return Speech(text, lang, None, vname, "client")


_tts: TTS | None = None


def get_tts() -> TTS:
    global _tts
    if _tts is None:
        _tts = TTS()
    return _tts
