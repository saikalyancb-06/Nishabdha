"""Offline backup engines (CPU): SpeechBrain ECAPA speaker embeddings + wav2vec2 anti-spoof ensemble.

Measured in the sandbox (40 LibriSpeech speakers, 3 s chunks): ECAPA genuine cosine >= 0.45,
impostor median 0.08 -> FAR 0.3% / FRR 0% at 0.40 - strong.
Anti-spoof: 5 open HF models benchmarked on 3 s chunks (74 real / 23 edge-tts). Best high-precision
option (MelodyMachine) flags ~1% of real chunks and ~30% of TTS chunks - much weaker than a commercial
API. That is why it is only the *backup*, and why no decision rests on the deepfake score alone
(speaker match + random-digit liveness with timing + app step-up).
"""
from __future__ import annotations

import logging
import os
import threading
import time

import numpy as np

from app.config import Settings
from app.providers.base import ProviderError, SpeakerResult, SpoofResult, clamp01

log = logging.getLogger("unclone.local")


def _limit_threads() -> None:
    try:
        import torch

        torch.set_num_threads(max(1, (os.cpu_count() or 2) - 1))
    except Exception:  # pragma: no cover
        pass


class LocalSpeaker:
    def __init__(self, s: Settings):
        self.s = s
        self._model = None
        self._lock = threading.Lock()
        self.load_error = ""

    def _load(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is None:
                try:
                    _limit_threads()
                    from speechbrain.inference.speaker import EncoderClassifier
                    from speechbrain.utils.fetching import LocalStrategy

                    dev = self.s.resolved_device
                    try:
                        self._model = EncoderClassifier.from_hparams(
                            source=self.s.local_speaker_model,
                            savedir=str(self.s.model_dir / "ecapa"),
                            run_opts={"device": dev},
                            local_strategy=LocalStrategy.COPY,  # Windows: no symlink privileges needed
                        )
                    except Exception as e:
                        if dev != "cpu":
                            log.warning("speaker model failed on %s (%s), falling back to cpu", dev, e)
                            self._model = EncoderClassifier.from_hparams(
                                source=self.s.local_speaker_model,
                                savedir=str(self.s.model_dir / "ecapa"),
                                run_opts={"device": "cpu"},
                                local_strategy=LocalStrategy.COPY,
                            )
                        else:
                            raise
                except Exception as e:  # pragma: no cover - depends on env
                    self.load_error = f"{type(e).__name__}: {e}"
                    raise ProviderError(f"local speaker model unavailable: {self.load_error}") from e
        return self._model

    def embed(self, audio: np.ndarray) -> np.ndarray:
        import torch

        if audio.size < 16000 * 0.5:
            raise ProviderError("audio too short for speaker embedding")
        model = self._load()
        with self._lock, torch.inference_mode():
            e = model.encode_batch(torch.from_numpy(audio.astype(np.float32))[None]).squeeze().cpu().numpy()
        n = np.linalg.norm(e)
        if not np.isfinite(n) or n == 0:
            raise ProviderError("invalid embedding")
        return (e / n).astype(np.float32)

    def calibrate(self, cos: float) -> float:
        lo, hi = self.s.ecapa_cos_low, self.s.ecapa_cos_high
        return clamp01((cos - lo) / max(hi - lo, 1e-6))

    def verify(self, enrolled: np.ndarray, audio: np.ndarray) -> SpeakerResult:
        t0 = time.perf_counter()
        e = self.embed(audio)
        cos = float(np.dot(enrolled, e))
        return SpeakerResult(prob=self.calibrate(cos), raw=cos, source="local", latency_ms=int((time.perf_counter() - t0) * 1000), embedding=e)

    def centroid(self, clips: list[np.ndarray]) -> np.ndarray:
        embs = [self.embed(c) for c in clips]
        c = np.mean(embs, axis=0)
        return (c / np.linalg.norm(c)).astype(np.float32)


_FAKE_WORDS = ("fake", "spoof", "synthetic", "deepfake", "ai", "generated")
_REAL_WORDS = ("real", "bonafide", "bona-fide", "genuine", "human")


def _p_fake(scores: list[dict]) -> float:
    by = {d["label"].lower(): float(d["score"]) for d in scores}
    for lab, v in by.items():
        if any(w == lab or lab.startswith(w) for w in _FAKE_WORDS):
            return v
    for lab, v in by.items():
        if any(w == lab or lab.startswith(w) for w in _REAL_WORDS):
            return 1.0 - v
    raise ProviderError(f"unknown spoof labels {list(by)}")


class LocalSpoof:
    MAX_SECONDS = 6.0

    def __init__(self, s: Settings):
        self.s = s
        self._pipes: dict[str, object] = {}
        self._lock = threading.Lock()
        self.load_errors: dict[str, str] = {}

    def _load(self) -> dict[str, object]:
        if self._pipes:
            return self._pipes
        with self._lock:
            if not self._pipes:
                _limit_threads()
                from transformers import pipeline

                dev = 0 if self.s.resolved_device == "cuda" else -1
                for m in self.s.spoof_model_list:
                    try:
                        self._pipes[m] = pipeline("audio-classification", model=m, device=dev, model_kwargs={"cache_dir": str(self.s.model_dir / "hf")})
                    except Exception as e:
                        if dev != -1:
                            log.warning("spoof model %s failed on GPU (%s), falling back to CPU", m, e)
                            try:
                                self._pipes[m] = pipeline("audio-classification", model=m, device=-1, model_kwargs={"cache_dir": str(self.s.model_dir / "hf")})
                            except Exception as e2:  # pragma: no cover
                                self.load_errors[m] = f"{type(e2).__name__}: {e2}"
                                log.error("spoof model %s failed to load: %s", m, e2)
                        else:  # pragma: no cover
                            self.load_errors[m] = f"{type(e).__name__}: {e}"
                            log.error("spoof model %s failed to load: %s", m, e)
                if not self._pipes:
                    raise ProviderError(f"no local spoof model could be loaded: {self.load_errors}")
        return self._pipes

    def calibrate(self, mean_fake: float) -> float:
        th = min(max(self.s.local_spoof_threshold, 0.05), 0.95)
        if mean_fake < th:
            return clamp01(0.5 * mean_fake / th)
        return clamp01(0.5 + 0.5 * (mean_fake - th) / (1 - th))

    def raw_scores(self, audio: np.ndarray) -> dict[str, float]:
        pipes = self._load()
        x = audio[: int(16000 * self.MAX_SECONDS)].astype(np.float32)
        out = {}
        with self._lock:
            for name, p in pipes.items():
                try:
                    out[name] = round(_p_fake(p({"raw": x, "sampling_rate": 16000}, top_k=None)), 4)
                except Exception as e:  # one model failing must not kill the ensemble
                    log.warning("spoof model %s inference failed: %s", name, e)
        if not out:
            raise ProviderError("all local spoof models failed")
        return out

    def detect(self, audio: np.ndarray) -> SpoofResult:
        t0 = time.perf_counter()
        if audio.size < 16000 * 0.5:
            raise ProviderError("audio too short for spoof detection")
        raw = self.raw_scores(audio)
        mean = float(np.mean(list(raw.values())))
        return SpoofResult(synthetic_prob=self.calibrate(mean), source="local", latency_ms=int((time.perf_counter() - t0) * 1000), detail={"models": raw, "mean": round(mean, 4)})


class MockEngine:
    """Deterministic engine for tests / UI work without models. Speaker similarity uses a crude
    log-spectrum fingerprint; spoof score defaults low. Tests can force values via `force_*`."""

    def __init__(self):
        self.force_speaker: float | None = None
        self.force_spoof: float | None = None
        self.speaker_queue: list[float] = []
        self.spoof_queue: list[float] = []

    @staticmethod
    def embed(audio: np.ndarray) -> np.ndarray:
        if audio.size < 400:
            raise ProviderError("audio too short")
        n = 512
        frames = audio[: (audio.size // n) * n].reshape(-1, n)
        spec = np.log(np.abs(np.fft.rfft(frames * np.hanning(n), axis=1)) + 1e-6).mean(axis=0)
        bands = np.array([b.mean() for b in np.array_split(spec, 32)])
        bands = bands - bands.mean()
        nn = np.linalg.norm(bands)
        return (bands / nn if nn else bands).astype(np.float32)

    def verify(self, enrolled: np.ndarray, audio: np.ndarray) -> SpeakerResult:
        e = self.embed(audio)
        if self.speaker_queue:
            p = self.speaker_queue.pop(0)
        elif self.force_speaker is not None:
            p = self.force_speaker
        else:
            p = clamp01((float(np.dot(enrolled, e)) - 0.5) / 0.4) if enrolled.shape == e.shape else 0.0
        return SpeakerResult(prob=p, raw=p, source="mock", embedding=e)

    def detect(self, audio: np.ndarray) -> SpoofResult:
        if self.spoof_queue:
            p = self.spoof_queue.pop(0)
        else:
            p = self.force_spoof if self.force_spoof is not None else 0.05
        return SpoofResult(synthetic_prob=p, source="mock")

    def centroid(self, clips: list[np.ndarray]) -> np.ndarray:
        c = np.mean([self.embed(x) for x in clips], axis=0)
        return (c / (np.linalg.norm(c) or 1)).astype(np.float32)
