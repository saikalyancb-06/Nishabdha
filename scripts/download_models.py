"""Pre-download every local model BEFORE the hackathon (venue Wi-Fi is unreliable).

    python scripts/download_models.py

Downloads: SpeechBrain ECAPA (speaker), anti-spoof model(s), faster-whisper (STT fallback).
Then checks edge-tts, Groq and DetectifAI connectivity.
"""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np  # noqa: E402

from app.config import get_settings  # noqa: E402


def step(name, fn):
    t = time.time()
    try:
        out = fn()
        print(f"  [OK]   {name} ({time.time() - t:.1f}s) {out or ''}")
        return True
    except Exception as e:
        print(f"  [FAIL] {name}: {type(e).__name__}: {e}")
        return False


def main():
    s = get_settings()
    print(f"Model folder: {s.model_dir}")
    noise = (np.random.default_rng(0).standard_normal(16000 * 3) * 0.05).astype(np.float32)
    ok = True

    from app.providers.local_models import LocalSpeaker, LocalSpoof

    ok &= step("speaker model (ECAPA)", lambda: LocalSpeaker(s).embed(noise).shape)
    ok &= step(f"anti-spoof model(s) {s.spoof_model_list}", lambda: LocalSpoof(s).raw_scores(noise))

    def whisper():
        import os
        from faster_whisper import WhisperModel

        device = s.resolved_device
        compute_type = "float16" if device == "cuda" else "int8"
        if device == "cuda":
            try:
                import torch

                torch_lib = os.path.join(os.path.dirname(torch.__file__), "lib")
                if hasattr(os, "add_dll_directory") and os.path.isdir(torch_lib):
                    os.add_dll_directory(torch_lib)
                os.environ["PATH"] = torch_lib + os.pathsep + os.environ.get("PATH", "")
            except Exception:
                pass
        m = WhisperModel(s.local_whisper_model, device=device, compute_type=compute_type, download_root=str(s.model_dir / "whisper"))
        list(m.transcribe(noise, beam_size=1)[0])
        return f"{s.local_whisper_model} ({device})"

    ok &= step("faster-whisper (STT fallback)", whisper)

    async def tts():
        from app.providers.tts import TTS

        sp = await TTS(s).speak("Hello from Unclone Bank", "en")
        if sp.client_tts:
            raise RuntimeError("edge-tts unavailable (run: pip install -U edge-tts)")
        return f"{len(sp.audio_mp3)} bytes"

    step("edge-tts (internet)", lambda: asyncio.run(tts()))

    async def groq():
        if not s.effective_groq_key:
            raise RuntimeError("GROQ_API_KEY not set in .env")
        from app.providers.llm import LLM

        return await LLM(s).json_chat("Return JSON {\"ok\": true}", "ping")

    if s.llm_provider == "groq" or s.stt_provider == "groq" or s.groq_keys:
        step(f"Groq API ({len(s.groq_keys)} keys found)", lambda: asyncio.run(groq()))

    async def sarvam():
        if not s.effective_sarvam_key:
            raise RuntimeError("Sarvam key not set in .env")
        from app.providers.stt import STT
        tr = await STT(s).transcribe(noise, "en")
        return f"transcribe ok ({tr.source})"

    if s.effective_sarvam_key:
        step("Sarvam AI STT", lambda: asyncio.run(sarvam()))
    if s.detectifai_base_url:
        print("  DetectifAI configured - run scripts/check_detectifai.py with a voice clip")
    else:
        print("  DetectifAI not configured yet (fill DETECTIFAI_* in .env on day 1)")
    print("\nAll local models ready." if ok else "\nSome downloads failed - fix before the event!")


if __name__ == "__main__":
    main()
