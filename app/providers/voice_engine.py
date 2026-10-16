"""Voice security engine: DetectifAI first, local models as automatic fallback, with circuit breakers.

Every check returns which engine answered so the evidence report can show it.
If *no* engine can answer, the result carries `None` and the policy layer fails closed (step-up/refuse).
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.audio import decode_any
from app.config import Settings, get_settings
from app.providers.base import (
    CircuitBreaker,
    EnrollResult,
    ProviderError,
    SpeakerResult,
    SpoofResult,
    run_sync,
)
from app.providers.detectifai import DetectifAIClient
from app.providers.local_models import LocalSpeaker, LocalSpoof, MockEngine

log = logging.getLogger("unclone.voice")


@dataclass
class EnrollmentRef:
    """What we know about a customer's voiceprint (loaded from VoiceEnrollment row)."""

    customer_id: str
    detectifai_ref: str | None = None
    local_embedding: np.ndarray | None = None
    mock_embedding: np.ndarray | None = None
    sample_paths: list[str] = field(default_factory=list)


@dataclass
class VoiceCheckResult:
    speaker: SpeakerResult | None
    spoof: SpoofResult | None
    errors: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.speaker_prob is None or self.synthetic_prob is None:
            return "missing"
        s = get_settings()
        if (
            self.synthetic_prob >= s.synthetic_hard
            or self.speaker_prob < s.speaker_reject
        ):
            return "flagged"
        if self.provenance == "degraded":
            return "degraded"
        if self.provenance == "unknown":
            return "unknown"
        return "success"

    @property
    def provenance(self) -> str:
        if self.spoof is None:
            return "unknown"
        if (
            self.spoof.synthetic_prob is None
            or not math.isfinite(self.spoof.synthetic_prob)
            or not 0 <= self.spoof.synthetic_prob <= 1
        ):
            return "unknown"
        if self.speaker is not None:
            if (
                self.speaker.prob is None
                or not math.isfinite(self.speaker.prob)
                or not 0 <= self.speaker.prob <= 1
            ):
                return "unknown"
            sources = {self.speaker.source, self.spoof.source}
            if sources == {"detectifai"}:
                return "detectifai"
            if "mock" in sources:
                return "mock"
            return "degraded"
        if self.spoof.source == "detectifai":
            return "detectifai"
        if self.spoof.source == "mock":
            return "mock"
        return "degraded"

    @property
    def speaker_prob(self) -> float | None:
        score = None if self.speaker is None else self.speaker.prob
        return (
            score
            if score is not None and math.isfinite(score) and 0 <= score <= 1
            else None
        )

    @property
    def synthetic_prob(self) -> float | None:
        score = None if self.spoof is None else self.spoof.synthetic_prob
        return (
            score
            if score is not None and math.isfinite(score) and 0 <= score <= 1
            else None
        )


class VoiceEngine:
    def __init__(self, s: Settings | None = None):
        self.s = s or get_settings()
        self.detectifai = DetectifAIClient(self.s)
        self.local_speaker = LocalSpeaker(self.s)
        self.local_spoof = LocalSpoof(self.s)
        self.mock = MockEngine()
        self.breakers = {
            n: CircuitBreaker(
                n, self.s.breaker_fail_threshold, self.s.breaker_cooldown_s
            )
            for n in (
                "detectifai_speaker",
                "detectifai_spoof",
                "local_speaker",
                "local_spoof",
            )
        }
        self._ref_cache: dict[str, np.ndarray] = {}

    @property
    def chain(self) -> list[str]:
        out = []
        for e in (self.s.voice_primary, self.s.voice_fallback):
            if e != "none" and e not in out:
                if e == "detectifai" and not self.detectifai.configured:
                    continue
                if e == "mock" and self.s.app_env not in ("test", "demo"):
                    continue
                out.append(e)
        return out

    # ------------------------------------------------------------ enrollment
    async def enroll(self, customer_id: str, clips: list[np.ndarray]) -> EnrollResult:
        joined = np.concatenate(clips)
        res = EnrollResult(source="")
        sources, errors = [], []
        for eng in self.chain:
            try:
                if eng == "detectifai":
                    ref, raw = await asyncio.wait_for(
                        self.detectifai.enroll(customer_id, joined),
                        self.s.voice_timeout_s * 2,
                    )
                    res.detectifai_ref = ref
                    res.detail["detectifai"] = (
                        raw if isinstance(raw, dict) else {"raw": str(raw)[:300]}
                    )
                elif eng == "local":
                    c = await run_sync(self.local_speaker.centroid, clips)
                    res.local_embedding = c.tobytes()
                elif eng == "mock":
                    res.detail["mock_embedding"] = self.mock.centroid(clips).tolist()
                sources.append(eng)
            except Exception as e:
                errors.append(f"{eng}: {type(e).__name__}: {e}"[:300])
                log.warning("enroll via %s failed: %s", eng, e)
        if not sources:
            raise ProviderError(
                "no voice engine could enroll this voice: " + "; ".join(errors)
            )
        res.source = "+".join(sources)
        res.detail["errors"] = errors
        return res

    # ------------------------------------------------------------ checks
    def _reference_audio(self, ref: EnrollmentRef) -> np.ndarray | None:
        if ref.customer_id in self._ref_cache:
            return self._ref_cache[ref.customer_id]
        clips = []
        for p in ref.sample_paths:
            try:
                clips.append(decode_any(Path(p).read_bytes()))
            except Exception:
                continue
        if not clips:
            return None
        audio = np.concatenate(clips)[: 16000 * 12]
        self._ref_cache[ref.customer_id] = audio
        return audio

    def forget(self, customer_id: str) -> None:
        self._ref_cache.pop(customer_id, None)

    async def _speaker(
        self, ref: EnrollmentRef, audio: np.ndarray, errors: list[str]
    ) -> SpeakerResult | None:
        for eng in self.chain:
            br = self.breakers.get(f"{eng}_speaker")
            if br and br.is_open:
                errors.append(f"{eng}_speaker skipped (circuit open)")
                continue
            try:
                if eng == "detectifai":
                    if self.s.detectifai_verify_mode == "pair":
                        r = await asyncio.wait_for(
                            self.detectifai.verify(
                                None, audio, await run_sync(self._reference_audio, ref)
                            ),
                            self.s.voice_timeout_s,
                        )
                    else:
                        if not ref.detectifai_ref:
                            raise ProviderError("customer not enrolled with DetectifAI")
                        r = await asyncio.wait_for(
                            self.detectifai.verify(ref.detectifai_ref, audio),
                            self.s.voice_timeout_s,
                        )
                elif eng == "local":
                    if ref.local_embedding is None:
                        raise ProviderError("customer has no local voiceprint")
                    r = await asyncio.wait_for(
                        run_sync(self.local_speaker.verify, ref.local_embedding, audio),
                        self.s.voice_timeout_s * 2,
                    )
                else:
                    if ref.mock_embedding is None:
                        raise ProviderError("customer has no mock voiceprint")
                    r = self.mock.verify(ref.mock_embedding, audio)
                if br:
                    br.ok()
                return r
            except Exception as e:
                # "not enrolled with this engine" is a data issue, not an outage - don't trip the breaker
                if br and not (
                    isinstance(e, ProviderError)
                    and (
                        "enrolled" in str(e)
                        or "voiceprint" in str(e)
                        or "too short" in str(e)
                    )
                ):
                    br.fail(e)
                errors.append(f"{eng}_speaker: {type(e).__name__}: {e}"[:300])
        return None

    async def _spoof(self, audio: np.ndarray, errors: list[str]) -> SpoofResult | None:
        for eng in self.chain:
            br = self.breakers.get(f"{eng}_spoof")
            if br and br.is_open:
                errors.append(f"{eng}_spoof skipped (circuit open)")
                continue
            try:
                if eng == "detectifai":
                    r = await asyncio.wait_for(
                        self.detectifai.deepfake(audio), self.s.voice_timeout_s
                    )
                elif eng == "local":
                    r = await asyncio.wait_for(
                        run_sync(self.local_spoof.detect, audio),
                        self.s.voice_timeout_s * 2,
                    )
                else:
                    r = self.mock.detect(audio)
                if br:
                    br.ok()
                return r
            except Exception as e:
                if br and not (isinstance(e, ProviderError) and "too short" in str(e)):
                    br.fail(e)
                errors.append(f"{eng}_spoof: {type(e).__name__}: {e}"[:300])
        return None

    async def check(
        self, ref: EnrollmentRef | None, audio: np.ndarray
    ) -> VoiceCheckResult:
        """Speaker verification + deepfake detection in parallel."""
        errors: list[str] = []
        spoof_task = asyncio.create_task(self._spoof(audio, errors))
        speaker = await self._speaker(ref, audio, errors) if ref is not None else None
        spoof = await spoof_task
        return VoiceCheckResult(speaker=speaker, spoof=spoof, errors=errors)

    async def warmup(self) -> dict:
        """Load local models at startup so the first call isn't slow."""
        out = {}
        if "local" in self.chain:
            silence = (
                np.random.default_rng(0).standard_normal(16000 * 2) * 0.01
            ).astype(np.float32)
            for name, fn in (
                ("local_speaker", self.local_speaker.embed),
                ("local_spoof", self.local_spoof.raw_scores),
            ):
                try:
                    await run_sync(fn, silence)
                    out[name] = "ready"
                except Exception as e:
                    out[name] = f"error: {e}"[:200]
        return out

    def status(self) -> dict:
        return {
            "chain": self.chain,
            "mode": "unavailable"
            if not self.chain
            else ("mock" if "mock" in self.chain else "real"),
            "detectifai_configured": self.detectifai.configured,
            "breakers": [b.status() for b in self.breakers.values()],
            "local_load_errors": {
                "speaker": self.local_speaker.load_error,
                "spoof": self.local_spoof.load_errors,
            },
        }

    async def close(self) -> None:
        await self.detectifai.close()


_engine: VoiceEngine | None = None


def get_voice_engine() -> VoiceEngine:
    global _engine
    if _engine is None:
        _engine = VoiceEngine()
    return _engine
