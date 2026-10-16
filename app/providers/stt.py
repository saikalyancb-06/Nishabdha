"""Speech-to-text: Groq Whisper large-v3-turbo (free tier, fast) with local faster-whisper fallback."""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from dataclasses import dataclass

import httpx
import numpy as np

from app.audio import to_wav_bytes
from app.config import Settings, get_settings
from app.i18n import detect_script_lang
from app.providers.base import CircuitBreaker, ProviderError, run_sync

log = logging.getLogger("unclone.stt")

_LANG_MAP = {"english": "en", "hindi": "hi", "kannada": "kn", "urdu": "hi", "en": "en", "hi": "hi", "kn": "kn", "ur": "hi"}
_HALLUCINATIONS = re.compile(
    r"^\s*(thank(s| you) for watching.*|subtitles? by.*|please subscribe.*|you\.?|\.+|music|\[.*\]|\(.*\))\s*$",
    re.I,
)


@dataclass
class Transcript:
    text: str
    lang: str  # en | hi | kn
    source: str
    latency_ms: int
    confidence: float = 1.0


def _norm_lang(lang: str | None, fallback: str) -> str:
    """Map Whisper's language name/code to en|hi|kn; anything else -> 'other' (so the caller can retry)."""
    if not lang:
        return fallback
    return _LANG_MAP.get(lang.lower(), "other")


class STT:
    def __init__(self, s: Settings | None = None):
        self.s = s or get_settings()
        self._local = None
        self._lock = threading.Lock()
        self.sarvam_breaker = CircuitBreaker("sarvam_stt", self.s.breaker_fail_threshold, self.s.breaker_cooldown_s)
        self.groq_breaker = CircuitBreaker("groq_stt", self.s.breaker_fail_threshold, self.s.breaker_cooldown_s)
        self.breaker = self.sarvam_breaker if self.s.stt_provider == "sarvam" else self.groq_breaker
        self._http: httpx.AsyncClient | None = None

    # ------------------------------------------------------------ sarvam
    async def _sarvam(self, audio: np.ndarray, lang_hint: str | None) -> Transcript:
        key = self.s.effective_sarvam_key
        if not key:
            raise ProviderError("SARVAM_AI_API / SARVAM_API_KEY not set")
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(self.s.stt_timeout_s, connect=4.0))
        t0 = time.perf_counter()
        sarvam_lang = {"en": "en-IN", "hi": "hi-IN", "kn": "kn-IN"}.get(lang_hint or "", "unknown")
        data = {
            "model": self.s.sarvam_stt_model,
            "language_code": sarvam_lang,
        }
        r = await self._http.post(
            "https://api.sarvam.ai/speech-to-text",
            headers={"api-subscription-key": key},
            files={"file": ("audio.wav", to_wav_bytes(audio), "audio/wav")},
            data=data,
        )
        if r.status_code >= 400:
            raise ProviderError(f"sarvam stt HTTP {r.status_code}: {r.text[:200]}")
        j = r.json()
        text = (j.get("transcript") or "").strip()
        resp_lang = j.get("language_code", "")
        lang_code = resp_lang.split("-")[0].lower() if resp_lang else (lang_hint or "en")
        return Transcript(text, _norm_lang(lang_code, lang_hint or "en"), "sarvam", int((time.perf_counter() - t0) * 1000), 1.0)

    # ------------------------------------------------------------ groq
    async def _groq(self, audio: np.ndarray, lang_hint: str | None) -> Transcript:
        keys = self.s.groq_keys
        if not keys:
            raise ProviderError("GROQ_API_KEY not set")
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(self.s.stt_timeout_s, connect=4.0))
        t0 = time.perf_counter()
        data = {"model": self.s.groq_stt_model, "response_format": "verbose_json", "temperature": "0"}
        if lang_hint:
            data["language"] = lang_hint
        wav = to_wav_bytes(audio)
        last_err: Exception | None = None
        for key in keys:
            r = await self._http.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": ("audio.wav", wav, "audio/wav")},
                data=data,
            )
            if r.status_code == 429 and len(keys) > 1:
                log.warning("Groq STT key rate-limited (429); failing over to next key")
                continue
            if r.status_code >= 400:
                last_err = ProviderError(f"groq stt HTTP {r.status_code}: {r.text[:200]}")
                continue
            j = r.json()
            text = (j.get("text") or "").strip()
            segs = j.get("segments") or []
            conf = 1.0
            if segs:
                nsp = [s.get("no_speech_prob", 0.0) for s in segs]
                conf = float(1.0 - np.mean(nsp))
            return Transcript(text, _norm_lang(j.get("language"), lang_hint or "en"), "groq", int((time.perf_counter() - t0) * 1000), conf)
        raise last_err or ProviderError("all Groq STT keys failed")

    # ------------------------------------------------------------ local
    def _local_model(self):
        if self._local is None:
            with self._lock:
                if self._local is None:
                    from faster_whisper import WhisperModel

                    import os

                    device = self.s.resolved_device
                    compute_type = "float16" if device == "cuda" else "int8"
                    cpu_threads = max(1, (os.cpu_count() or 2) // 2)
                    if device == "cuda":
                        try:
                            import torch

                            torch_lib = os.path.join(os.path.dirname(torch.__file__), "lib")
                            if hasattr(os, "add_dll_directory") and os.path.isdir(torch_lib):
                                os.add_dll_directory(torch_lib)
                            os.environ["PATH"] = torch_lib + os.pathsep + os.environ.get("PATH", "")
                        except Exception:
                            pass
                    try:
                        self._local = WhisperModel(
                            self.s.local_whisper_model,
                            device=device,
                            compute_type=compute_type,
                            download_root=str(self.s.model_dir / "whisper"),
                            cpu_threads=cpu_threads,
                        )
                    except Exception as e:
                        if device == "cuda":
                            log.warning("WhisperModel failed on CUDA (%s), falling back to CPU", e)
                            self._local = WhisperModel(
                                self.s.local_whisper_model,
                                device="cpu",
                                compute_type="int8",
                                download_root=str(self.s.model_dir / "whisper"),
                                cpu_threads=cpu_threads,
                            )
                        else:
                            raise
        return self._local

    def _local_run(self, audio: np.ndarray, lang_hint: str | None) -> Transcript:
        t0 = time.perf_counter()
        m = self._local_model()
        dur = audio.size / 16000
        with self._lock:
            segs, info = m.transcribe(
                audio.astype(np.float32),
                language=lang_hint,
                beam_size=1,
                temperature=0.0,
                condition_on_previous_text=False,  # prevents runaway repetition loops
                without_timestamps=True,
                max_new_tokens=min(440, int(dur * 30) + 30),
                vad_filter=False,
            )
            segs = list(segs)  # generator - must be consumed under the lock
        text = " ".join(s.text.strip() for s in segs).strip()
        conf = float(1.0 - np.mean([s.no_speech_prob for s in segs])) if segs else 0.0
        return Transcript(text, _norm_lang(info.language, lang_hint or "en"), "local", int((time.perf_counter() - t0) * 1000), conf)

    async def _local_tx(self, audio, lang_hint):
        return await asyncio.wait_for(run_sync(self._local_run, audio, lang_hint), self.s.stt_timeout_s * 3)

    # ------------------------------------------------------------ public
    async def transcribe(self, audio: np.ndarray, call_lang: str = "en", digits: bool = False) -> Transcript:
        """Constrain to en/hi/kn.
        - digits=True (liveness answers): no language hint - Whisper then writes '7 4 2 9' as numerals.
        - Kannada calls: hint 'kn' - auto-detect mistakes Kannada for English gibberish, while the hint keeps
          English words English (measured on Groq large-v3-turbo).
        - English/Hindi calls: auto-detect (a 'hi' hint garbles English speech)."""
        if audio.size < 16000 * 0.2:
            return Transcript("", call_lang, "none", 0, 0.0)
        order = []
        if self.s.stt_provider == "sarvam":
            order.append("sarvam")
        elif self.s.stt_provider == "groq":
            order.append("groq")
        elif self.s.stt_provider == "local":
            order.append("local")

        fallback = self.s.stt_fallback
        if fallback and fallback != "none" and fallback not in order:
            order.append(fallback)
        if "local" not in order:
            order.append("local")

        errors = []
        for eng in order:
            if eng == "sarvam" and self.sarvam_breaker.is_open:
                errors.append("sarvam skipped (circuit open)")
                continue
            if eng == "groq" and self.groq_breaker.is_open:
                errors.append("groq skipped (circuit open)")
                continue
            try:
                hint = "kn" if (call_lang == "kn" and not digits) else None
                if eng == "sarvam":
                    tr = await self._sarvam(audio, hint)
                    self.sarvam_breaker.ok()
                elif eng == "groq":
                    tr = await self._groq(audio, hint)
                    self.groq_breaker.ok()
                else:
                    tr = await self._local_tx(audio, hint)

                script = detect_script_lang(tr.text)
                if script:
                    tr.lang = script
                elif tr.lang not in ("en", "hi", "kn") and not (digits and any(ch.isdigit() for ch in tr.text)):
                    if eng == "sarvam":
                        tr = await self._sarvam(audio, call_lang)
                    elif eng == "groq":
                        tr = await self._groq(audio, call_lang)
                    else:
                        tr = await self._local_tx(audio, call_lang)
                    tr.lang = call_lang if tr.lang not in ("en", "hi", "kn") else tr.lang
                if _HALLUCINATIONS.match(tr.text) or tr.confidence < 0.2:
                    tr.text = ""
                return tr
            except Exception as e:
                if eng == "sarvam":
                    self.sarvam_breaker.fail(e)
                elif eng == "groq":
                    self.groq_breaker.fail(e)
                errors.append(f"{eng}: {type(e).__name__}: {e}"[:200])
                log.warning("STT %s failed: %s: %s", eng, type(e).__name__, e)
        raise ProviderError("all STT engines failed: " + "; ".join(errors))

    async def warmup(self) -> str:
        if "local" not in (self.s.stt_provider, self.s.stt_fallback):
            return "skipped"
        silence = np.zeros(16000, np.float32)
        await run_sync(self._local_run, silence, "en")
        return "ready"

    async def close(self):
        if self._http:
            await self._http.aclose()


_stt: STT | None = None


def get_stt() -> STT:
    global _stt
    if _stt is None:
        _stt = STT()
    return _stt
