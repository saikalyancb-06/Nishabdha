from __future__ import annotations

import json

import httpx
import numpy as np
import pytest

from app.audio import StreamSegmenter, assess, decode_any, envelope
from app.config import get_settings
from app.core.liveness import evaluate_answer, new_challenge_digits
from app.core.nlu import parse_amount_rupees, rule_nlu
from app.core.pressure import lexicon_score
from app.core.trust import ChunkScore, LivenessOutcome, TrustState, decide
from app.i18n import extract_digits, format_inr, speak_digits
from app.providers.detectifai import DetectifAIClient, json_path
from app.providers.voice_engine import EnrollmentRef, VoiceEngine

from .conftest import audio


# ---------------------------------------------------------------- language
@pytest.mark.parametrize(
    "text,expected",
    [
        ("seven four two nine", "7429"),
        ("7 4 2 9", "7429"),
        ("सात चार दो नौ", "7429"),
        ("saat char do nau", "7429"),
        ("ಏಳು ನಾಲ್ಕು ಎರಡು ಒಂಬತ್ತು", "7429"),
        ("elu naalku eradu ombattu", "7429"),
        ("double five three", "553"),
        ("७४२९", "7429"),
        ("೭೪೨೯", "7429"),
        ("", ""),
    ],
)
def test_extract_digits(text, expected):
    assert extract_digits(text) == expected


def test_speak_digits_roundtrip():
    for lang in ("en", "hi", "kn"):
        assert extract_digits(speak_digits("7429", lang)) == "7429"


@pytest.mark.parametrize(
    "text,val",
    [
        ("50,000", 50000),
        ("50k", 50000),
        ("fifty thousand", 50000),
        ("1.5 lakh", 150000),
        ("₹2000", 2000),
        ("pachaas hazaar", 50000),
        ("2 लाख", 200000),
        ("no number", None),
    ],
)
def test_parse_amount(text, val):
    assert parse_amount_rupees(text) == val


def test_format_inr():
    assert format_inr(1_24_500_00) == "Rs 1,24,500"
    assert format_inr(-500_00) == "-Rs 500"


@pytest.mark.parametrize(
    "text,intent",
    [
        ("please block my card", "card_block"),
        ("I lost my wallet, freeze my card", "card_freeze"),
        ("unblock my debit card", "card_unfreeze"),
        ("what's my balance", "balance"),
        ("show my recent transactions", "recent_transactions"),
        ("change my mobile number", "update_mobile"),
        ("increase my card limit to 1 lakh", "card_limit_change"),
        ("enable international usage", "card_international"),
        ("add a new payee", "add_payee"),
        ("send 500 rupees to Amma", "transfer_funds"),
        ("is this really the bank?", "verify_bank"),
        ("मेरा बैलेंस बताइए", "balance"),
        ("मेरा कार्ड ब्लॉक कर दो", "card_block"),
        ("ನನ್ನ ಡೆಬಿಟ್ ಕಾರ್ಡ್ ಬ್ಲಾಕ್ ಮಾಡಿ", "card_block"),
        ("ನನ್ನ ಬ್ಯಾಲೆನ್ಸ್ ಎಷ್ಟು", "balance"),
        ("what are your branch timings", "faq"),
        ("bye", "goodbye"),
    ],
)
def test_rule_intents(text, intent):
    assert rule_nlu(text).intent == intent


def test_rule_params():
    r = rule_nlu("change limit on card ending 8890 to 75,000")
    assert r.params == {"card_last4": "8890", "new_limit_rupees": 75000}
    r = rule_nlu("update my mobile number to 98450 12345")
    assert r.params["new_mobile"] == "9845012345"
    r = rule_nlu("disable international use on my credit card")
    assert r.params["international"] is False and r.params["card_kind"] == "credit"


def test_pressure_lexicon():
    assert lexicon_score("what is my balance").score == 0
    p = lexicon_score(
        "The police will arrest you, move money to a safe account right now and don't tell anyone"
    )
    assert p.score >= 0.9 and {
        "safe_account",
        "authority_threat",
        "urgency",
        "secrecy",
    } <= set(p.cues)
    assert lexicon_score("पुलिस ने कहा तुरंत पैसे भेजो").score >= 0.5


# ---------------------------------------------------------------- liveness
def test_challenge_digits_random_and_distinct():
    seen = {new_challenge_digits(4) for _ in range(200)}
    assert len(seen) > 150
    for c in seen:
        assert all(a != b for a, b in zip(c, c[1:], strict=False))
    assert new_challenge_digits(4, avoid="1234") != "1234"


def test_evaluate_answer():
    assert evaluate_answer("7429", "seven four two nine", 1.0, False).passed
    assert not evaluate_answer("7429", "seven four two eight", 1.0, False).passed
    assert not evaluate_answer(
        "7429", "0 1 2 3 4 5 6 7 4 2 9 8 7", 1.0, False
    ).passed  # digit spraying
    assert evaluate_answer(
        "7429", "seven four two nine", -2.0, False
    ).passed  # playback estimate is advisory
    assert not evaluate_answer(
        "7429", "seven four two nine", 1.0, True
    ).passed  # expired
    slow = evaluate_answer("7429", "7429", 6.0, False)
    assert slow.passed and "slow" in slow.reasons[0]


# ---------------------------------------------------------------- trust / decision
def _state(*chunks):
    st = TrustState(get_settings())
    for i, (sp, sy) in enumerate(chunks):
        st.add(ChunkScore(i + 1, i * 3.0, i * 3.0 + 3, sp, sy))
    return st


def test_decide_matrix():
    s = get_settings()
    good = _state((0.9, 0.05), (0.92, 0.03))
    live_ok = LivenessOutcome(True, 1.0, 0.9, 0.05)
    assert (
        decide(0, good, enrolled=True, voice_locked=False, pressure=0).outcome
        == "allow"
    )
    assert (
        decide(1, good, enrolled=True, voice_locked=False, pressure=0).outcome
        == "allow"
    )
    assert (
        decide(2, good, enrolled=True, voice_locked=False, pressure=0).outcome
        == "challenge"
    )
    assert (
        decide(
            2, good, enrolled=True, voice_locked=False, pressure=0, liveness=live_ok
        ).outcome
        == "allow"
    )
    # pressure forces step-up even with good voice
    assert (
        decide(
            2, good, enrolled=True, voice_locked=False, pressure=0.8, liveness=live_ok
        ).outcome
        == "stepup"
    )
    # Timing alone cannot identify a clone or authorize/refuse a request.
    assert (
        decide(
            2,
            good,
            enrolled=True,
            voice_locked=False,
            pressure=0,
            liveness=LivenessOutcome(True, 6.0, 0.9, 0.05),
        ).outcome
        == "allow"
    )
    # liveness audio by a different speaker -> refuse
    assert (
        decide(
            2,
            good,
            enrolled=True,
            voice_locked=False,
            pressure=0,
            liveness=LivenessOutcome(True, 1.0, 0.1, 0.05),
        ).outcome
        == "refuse"
    )
    # engines down on challenge audio -> fail closed to step-up (never allow)
    assert (
        decide(
            2,
            good,
            enrolled=True,
            voice_locked=False,
            pressure=0,
            liveness=LivenessOutcome(True, 1.0, None, None),
        ).outcome
        == "stepup"
    )
    # unenrolled caller: tier 2 requires independent step-up verification
    assert (
        decide(2, good, enrolled=False, voice_locked=False, pressure=0).outcome
        == "stepup"
    )
    # voice locked -> refuse
    assert (
        decide(1, good, enrolled=True, voice_locked=True, pressure=0).outcome
        == "refuse"
    )
    # synthetic anywhere -> refuse, even tier 1
    fake = _state((0.95, 0.05), (0.95, s.synthetic_hard + 0.05))
    assert (
        decide(1, fake, enrolled=True, voice_locked=False, pressure=0).outcome
        == "refuse"
    )
    assert fake.locked_reason
    # mismatched voice tier 1 -> step-up, not data leak
    bad = _state((0.05, 0.02))
    assert (
        decide(1, bad, enrolled=True, voice_locked=False, pressure=0).outcome
        == "stepup"
    )


def test_swap_detection():
    st = _state((0.9, 0.05), (0.93, 0.04), (0.91, 0.05))
    assert st.swap is None
    flags = st.add(ChunkScore(4, 9, 12, 0.15, 0.05))
    assert "voice_swap" in flags and st.swap is not None and st.locked_reason
    # a dip that recovers (cough / noise) is not a swap
    st2 = _state((0.9, 0.05), (0.92, 0.05), (0.75, 0.05), (0.9, 0.05))
    assert st2.swap is None
    # synthetic after a clean baseline also counts as a swap
    st3 = _state((0.9, 0.05), (0.92, 0.05), (0.9, 0.95))
    assert st3.swap is not None


# ---------------------------------------------------------------- audio
def test_audio_pipeline_real_speech():
    x = audio("real_1089_3.wav")
    q = assess(x)
    assert q.ok and q.speech_s > 2
    seg = StreamSegmenter(chunk_s=3.0)
    utts, chunks = [], []
    for i in range(0, x.size, 1600):
        u, c = seg.feed(x[i : i + 1600])
        utts += u
        chunks += c
    u, c = seg.flush()
    utts += u
    chunks += c
    assert chunks and all(abs((b - a) - 3.0) < 1.5 for _, a, b in chunks[:-1])
    assert utts
    assert len(envelope(x, 100)) == 100
    assert not assess(np.zeros(32000, np.float32)).ok


def test_decode_mp3_and_garbage():
    from app.audio import AudioError

    with pytest.raises(AudioError):
        decode_any(b"garbage" * 100)


# ---------------------------------------------------------------- DetectifAI adapter
def test_json_path():
    assert json_path({"a": {"b": [{"c": 0.7}]}}, "a.b.0.c") == 0.7


@pytest.mark.asyncio
async def test_detectifai_adapter_configurable(monkeypatch):
    s = get_settings().model_copy(
        update={
            "detectifai_base_url": "https://api.detectif.test",
            "detectifai_api_key": "k",
            "detectifai_auth_header": "x-api-key",
            "detectifai_auth_scheme": "",
            "detectifai_verify_score_path": "result.similarity",
            "detectifai_verify_score_max": 100.0,
            "detectifai_deepfake_score_path": "data.real_prob",
            "detectifai_deepfake_score_is_real": True,
        }
    )
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        assert req.headers["x-api-key"] == "k"
        if req.url.path.endswith("verify"):
            return httpx.Response(200, json={"result": {"similarity": "87%"}})
        if req.url.path.endswith("detect"):
            return httpx.Response(200, json={"data": {"real_prob": 0.1}})
        return httpx.Response(200, json={"speaker_id": "spk_1"})

    cl = DetectifAIClient(s)
    cl._client = httpx.AsyncClient(
        base_url=s.detectifai_base_url,
        headers={"x-api-key": "k"},
        transport=httpx.MockTransport(handler),
    )
    x = audio("real_1089_3.wav")
    ref, _ = await cl.enroll("cus_1", x)
    assert ref == "spk_1"
    r = await cl.verify(ref, x)
    assert abs(r.prob - 0.87) < 1e-6
    d = await cl.deepfake(x)
    assert abs(d.synthetic_prob - 0.9) < 1e-6
    assert b"audio.wav" in seen[1].content


@pytest.mark.asyncio
async def test_engine_falls_back_when_detectifai_down():
    s = get_settings().model_copy(
        update={
            "voice_primary": "detectifai",
            "voice_fallback": "mock",
            "detectifai_base_url": "https://down.test",
            "breaker_fail_threshold": 2,
        }
    )
    eng = VoiceEngine(s)
    eng.detectifai._client = httpx.AsyncClient(
        base_url="https://down.test",
        transport=httpx.MockTransport(lambda r: httpx.Response(503, text="down")),
    )
    x = audio("real_1089_3.wav")
    ref = EnrollmentRef("c", detectifai_ref="spk", mock_embedding=eng.mock.embed(x))
    r1 = await eng.check(ref, x)
    assert r1.speaker.source == "mock" and r1.spoof.source == "mock"
    assert any("detectifai" in e for e in r1.errors)
    await eng.check(ref, x)
    assert eng.breakers[
        "detectifai_speaker"
    ].is_open  # third call skips the dead API entirely
    r3 = await eng.check(ref, x)
    assert any("circuit open" in e for e in r3.errors)


@pytest.mark.asyncio
async def test_engine_all_down_returns_none():
    s = get_settings().model_copy(
        update={
            "voice_primary": "detectifai",
            "voice_fallback": "none",
            "detectifai_base_url": "https://down.test",
        }
    )
    eng = VoiceEngine(s)
    eng.detectifai._client = httpx.AsyncClient(
        base_url="https://down.test",
        transport=httpx.MockTransport(lambda r: httpx.Response(500)),
    )
    r = await eng.check(
        EnrollmentRef("c", detectifai_ref="x"), audio("real_1089_3.wav")
    )
    assert r.speaker is None and r.spoof is None  # policy then fails closed


def test_llm_output_validation():
    from app.core.nlu import _validate

    r = _validate(
        {
            "intent": "transfer_everything_now",
            "params": {
                "amount_rupees": "1e99",
                "new_mobile": "12345",
                "card_last4": "45210",
            },
            "pressure": "high",
        },
        "x",
    )
    assert r.intent == "unknown" and r.params == {} and r.pressure.score == 0
    r = _validate(
        {
            "intent": "card_limit_change",
            "params": {"new_limit_rupees": "75,000", "card_last4": "8890"},
            "pressure": 0.2,
            "reply": "sure!",
        },
        "x",
    )
    assert (
        r.params == {"new_limit_rupees": 75000, "card_last4": "8890"} and r.reply == ""
    )  # no free text for actions
    assert json.dumps(r.params)
