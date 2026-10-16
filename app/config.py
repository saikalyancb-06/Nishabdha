"""Central configuration. Every value can be overridden in `.env` or environment variables."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ---------------- App ----------------
    app_name: str = "UNCLONE Voice Banking Guard"
    app_env: Literal["dev", "demo", "test"] = "dev"
    host: str = "0.0.0.0"
    port: int = 8000
    cors_origins: str = "*"  # comma separated
    staff_api_key: str = (
        "change-me-staff-key"  # fraud-desk / admin endpoints (header X-Staff-Key)
    )
    audit_hmac_secret: str = "change-me-audit-secret"  # signs the audit hash chain
    data_dir: Path = BASE_DIR / "data"
    database_url: str = ""  # default -> sqlite file in data_dir
    log_level: str = "INFO"
    bank_origin: str = "http://localhost:8000"
    webauthn_rp_id: str = "localhost"

    # ---------------- LLM ----------------
    llm_provider: Literal["groq", "openai", "anthropic", "gemini", "none"] = "groq"
    llm_model: str = ""  # empty -> provider default
    llm_timeout_s: float = 8.0
    groq_api_key: str = ""
    groq_api_key1: str = ""
    groq_api_key2: str = ""
    groq_api_key3: str = ""
    openai_api_key: str = ""
    anthropic_api_key: str = ""
    gemini_api_key: str = ""
    sarvam_api_key: str = ""
    sarvam_ai_api: str = ""

    # ---------------- Speech-to-text ----------------
    stt_provider: Literal["sarvam", "groq", "local", "none"] = "sarvam"
    stt_fallback: Literal["groq", "local", "none"] = "groq"
    groq_stt_model: str = "whisper-large-v3-turbo"
    sarvam_stt_model: str = "saaras:v4"
    local_whisper_model: str = "small"  # tiny/base/small/medium (multilingual)
    stt_timeout_s: float = 12.0

    # ---------------- Text-to-speech ----------------
    tts_provider: Literal["sarvam", "edge", "none"] = "sarvam"
    tts_timeout_s: float = (
        15.0  # slow venue Wi-Fi: edge-tts can take >8 s for the first byte
    )
    sarvam_tts_model: str = "bulbul:v3"
    sarvam_tts_speaker: str = "shreya"
    tts_voice_en: str = "en-IN-NeerjaNeural"
    tts_voice_hi: str = "hi-IN-SwaraNeural"
    tts_voice_kn: str = "kn-IN-SapnaNeural"

    # ---------------- Voice security engines ----------------
    voice_primary: Literal["detectifai", "local", "mock"] = "detectifai"
    voice_fallback: Literal["local", "mock", "none"] = "local"
    voice_timeout_s: float = 6.0
    breaker_fail_threshold: int = 3  # consecutive failures before primary is skipped
    breaker_cooldown_s: float = 30.0

    # DetectifAI adapter (fill on day 1 from their docs - no code change needed for most APIs)
    detectifai_base_url: str = ""
    detectifai_api_key: str = ""
    detectifai_auth_header: str = "Authorization"
    detectifai_auth_scheme: str = "Bearer"  # set "" to send the raw key
    detectifai_upload_mode: Literal["multipart", "base64_json"] = "multipart"
    detectifai_audio_field: str = "file"
    detectifai_speaker_field: str = "speaker_id"
    detectifai_reference_field: str = "reference"
    detectifai_verify_mode: Literal["enrolled_id", "pair"] = "enrolled_id"
    detectifai_enroll_path: str = "/v1/speaker/enroll"
    detectifai_verify_path: str = "/v1/speaker/verify"
    detectifai_deepfake_path: str = "/v1/deepfake/detect"
    detectifai_enroll_id_path: str = "speaker_id"  # dotted JSON path in enroll response
    detectifai_verify_score_path: str = "score"
    detectifai_verify_score_max: float = 1.0  # 100 if API returns percentages
    detectifai_deepfake_score_path: str = "fake_probability"
    detectifai_deepfake_score_is_real: bool = False  # True if API returns P(real)
    detectifai_deepfake_score_max: float = 1.0
    detectifai_extra_json: str = ""  # optional JSON merged into every request body/form

    # Local backup models
    device: Literal["auto", "cuda", "cpu"] = "auto"
    model_dir: Path = BASE_DIR / "models"
    local_speaker_model: str = "speechbrain/spkrec-ecapa-voxceleb"
    # Benchmarked 5 open models on 3 s chunks (74 real / 23 TTS): open detectors are weak on short chunks.
    # MelodyMachine is the high-precision choice (~1% false alarms, catches ~30% of TTS chunks); it is a
    # BACKUP - DetectifAI is primary, and liveness timing + speaker match + step-up cover the rest.
    local_spoof_models: str = "MelodyMachine/Deepfake-audio-detection-V2"
    local_spoof_threshold: float = 0.5  # ensemble mean P(fake) mapped to 0.5
    # ECAPA cosine calibration (measured: genuine >= 0.45, impostor median 0.08 on 3 s chunks)
    ecapa_cos_low: float = 0.25  # maps to match prob 0
    ecapa_cos_high: float = 0.55  # maps to match prob 1

    # ---------------- Trust / policy thresholds ----------------
    chunk_seconds: float = 3.0  # speech seconds per continuous voice check
    min_chunk_seconds: float = 1.5
    trust_allow: float = 0.72  # >= allow (tier 2 also needs liveness)
    trust_stepup: float = (
        0.45  # between stepup and allow -> step-up; below -> refuse for tier 2
    )
    tier1_min_trust: float = 0.5
    speaker_reject: float = (
        0.30  # speaker prob below this on challenge audio = mismatch
    )
    synthetic_hard: float = 0.80  # P(fake) at/above -> synthetic flag (hard refuse)
    synthetic_soft: float = 0.55  # suspicious -> step-up
    pressure_boost: float = 0.10  # extra trust required under pressure tactics
    min_evidence_chunks: int = 1
    swap_drop: float = 0.35  # speaker prob drop vs baseline that signals a swap
    swap_min_baseline_chunks: int = 2

    # Liveness challenge
    challenge_digits: int = 4
    challenge_ttl_s: float = 30.0
    challenge_max_attempts: int = 2
    challenge_slow_latency_s: float = (
        4.0  # response onset slower than this = suspicious
    )
    challenge_min_latency_s: float = (
        0.15  # faster than this = audio started before prompt (replay)
    )

    # Step-up / OTP
    otp_ttl_s: float = 120.0
    otp_max_attempts: int = 3
    payee_cooling_hours: int = 24
    max_transfer_paise: int = 50_000_00  # Rs 50,000 per call
    bank_call_code_ttl_s: float = 600.0

    # Enrollment
    enroll_min_samples: int = 3
    enroll_min_seconds: float = 2.0  # speech per sample
    enroll_min_rms_dbfs: float = -45.0

    # Realtime / VAD
    vad_aggressiveness: int = Field(2, ge=0, le=3)
    vad_end_silence_ms: int = 700
    vad_min_utterance_ms: int = 300
    vad_max_utterance_s: float = 15.0
    max_ws_frame_bytes: int = 64_000
    max_upload_bytes: int = 15 * 1024 * 1024

    @field_validator("data_dir", "model_dir", mode="after")
    @classmethod
    def _abs(cls, v: Path) -> Path:
        v = Path(v)
        return v if v.is_absolute() else (BASE_DIR / v)

    @property
    def db_url(self) -> str:
        return (
            self.database_url
            or f"sqlite+aiosqlite:///{(self.data_dir / 'unclone.db').as_posix()}"
        )

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def spoof_model_list(self) -> list[str]:
        return [m.strip() for m in self.local_spoof_models.split(",") if m.strip()]

    @property
    def resolved_device(self) -> str:
        if self.device == "auto":
            try:
                import torch

                return "cuda" if torch.cuda.is_available() else "cpu"
            except Exception:
                return "cpu"
        return self.device

    @property
    def effective_sarvam_key(self) -> str:
        return (self.sarvam_api_key or self.sarvam_ai_api or "").strip()

    @property
    def groq_keys(self) -> list[str]:
        keys = []
        for k in (self.groq_api_key, self.groq_api_key1, self.groq_api_key2, self.groq_api_key3):
            if k and k.strip() and k.strip() not in keys:
                keys.append(k.strip())
        return keys

    @property
    def effective_groq_key(self) -> str:
        keys = self.groq_keys
        return keys[0] if keys else ""

    def voice_for(self, lang: str) -> str:
        return {"hi": self.tts_voice_hi, "kn": self.tts_voice_kn}.get(
            lang, self.tts_voice_en
        )


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    s.data_dir.mkdir(parents=True, exist_ok=True)
    (s.data_dir / "clips").mkdir(exist_ok=True)
    (s.data_dir / "enroll").mkdir(exist_ok=True)
    s.model_dir.mkdir(parents=True, exist_ok=True)
    return s
