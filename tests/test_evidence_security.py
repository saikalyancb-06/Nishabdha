"""Current utterance evidence cannot be substituted by historic trust."""

import asyncio
import time
from unittest.mock import AsyncMock

import numpy as np
import pytest

from app.config import get_settings
from app.core.session import CallSession, Pending, PlaybackInfo
from app.core.trust import ChunkScore, LivenessOutcome, TrustState, decide
from app.providers.base import SpeakerResult, SpoofResult
from app.providers.voice_engine import VoiceCheckResult, VoiceEngine


def current_request(sess, source="mock"):
    p = Pending(
        "action-1", "transfer_funds", {"payee_name": "Amma", "amount_rupees": 5}, 2
    )
    p.request_audio_hash = "current-pcm"
    p.evidence_status = source
    p.evidence_checked_at = time.monotonic()
    p.request_evidence = {
        "call_id": sess.call_id,
        "action_id": p.action_id,
        "audio_hash": p.request_audio_hash,
        "speaker_prob": 0.95,
        "synthetic_prob": 0.02,
    }
    sess.pending = p
    return p


@pytest.mark.asyncio
async def test_current_check_timeout_clears_old_request_and_historic_trust(monkeypatch):
    sess = CallSession("timeout")
    sess.enrolled = True
    current_request(sess)
    sess.trust.add(ChunkScore(1, 0, 3, 0.99, 0.01))
    monkeypatch.setattr(sess, "_drain_checks", AsyncMock())
    monkeypatch.setattr(sess, "run_check", AsyncMock(side_effect=asyncio.TimeoutError))
    assert not await sess._ensure_evidence(np.ones(16000 * 3, dtype=np.float32), 5, 8)
    assert not sess._request_evidence_valid(sess.pending, transfer=True)
    assert sess.pending.evidence_status == "unknown"
    assert sess.trust.trust > 0.9


@pytest.mark.asyncio
async def test_typed_request_cannot_reuse_previous_voice(monkeypatch):
    sess = CallSession("typed")
    sess.enrolled = True
    current_request(sess)
    assert not await sess._ensure_evidence(None, 0, 0)
    assert sess.pending.request_evidence == {}
    assert not sess._request_evidence_valid(sess.pending, transfer=True)


@pytest.mark.parametrize("source", ["degraded", "unknown"])
def test_local_or_incomplete_evidence_never_authorizes_transfer(source):
    sess = CallSession("fallback")
    p = current_request(sess, source)
    assert not sess._request_evidence_valid(p, transfer=True)


def test_evidence_requires_exact_action_audio_and_freshness():
    sess = CallSession("bound")
    sess.enrolled = True
    p = current_request(sess)
    assert sess._request_evidence_valid(p, transfer=True)
    p.request_audio_hash = "different-pcm"
    assert not sess._request_evidence_valid(p, transfer=True)
    p.request_audio_hash = "current-pcm"
    p.evidence_checked_at -= 121
    assert not sess._request_evidence_valid(p, transfer=True)


def test_nonfinite_or_missing_scores_are_unknown():
    res = VoiceCheckResult(
        SpeakerResult(float("nan"), 0, "detectifai"), SpoofResult(0.01, "detectifai")
    )
    assert res.provenance == "unknown"
    assert res.speaker_prob is None
    assert (
        VoiceCheckResult(SpeakerResult(0.9, 0, "detectifai"), None).provenance == "unknown"
    )
    assert VoiceCheckResult(None, None).provenance == "unknown"
    # When speaker verification is unavailable (no voice registration), authenticity engine provides provenance
    assert (
        VoiceCheckResult(None, SpoofResult(0.01, "detectifai")).provenance == "detectifai"
    )


@pytest.mark.asyncio
async def test_empty_engine_chain_returns_unavailable():
    engine = VoiceEngine(
        get_settings().model_copy(
            update={"voice_primary": "none", "voice_fallback": "none"}
        )
    )
    try:
        assert engine.chain == []
        res = await engine.check(None, np.ones(16000, dtype=np.float32))
        assert res.speaker is None and res.spoof is None
        assert engine.status()["mode"] == "unavailable"
    finally:
        await engine.close()


def test_mock_is_explicitly_disallowed_in_development():
    engine = VoiceEngine(
        get_settings().model_copy(
            update={"app_env": "dev", "voice_primary": "mock", "voice_fallback": "none"}
        )
    )
    assert engine.chain == []


def test_repeated_playback_events_cannot_reset_timing():
    sess = CallSession("playback")
    sess._playback["m1"] = PlaybackInfo(123.0, 4.0)
    sess.playback_event("m1", "started")
    sess.playback_event("m1", "ended")
    first_done = sess._playback["m1"].done_wall
    sess.playback_event("m1", "started")
    sess.playback_event("m1", "ended")
    assert sess._playback["m1"].sent_wall == 123.0
    assert sess._playback["m1"].done_wall == first_done


@pytest.mark.asyncio
async def test_inbound_verify_bank_cannot_issue_outbound_notice(monkeypatch):
    sess = CallSession("inbound", customer_id="cus_ananya")
    say = AsyncMock()
    monkeypatch.setattr(sess, "say", say)
    await sess._verify_bank()
    assert "inbound" in say.call_args.args[0].lower()


@pytest.mark.asyncio
async def test_token_only_transfer_approval_cannot_execute(monkeypatch):
    sess = CallSession("passkey")
    p = current_request(sess)
    p.stage, p.stepup_id = "stepup", "step-1"
    execute = AsyncMock()
    monkeypatch.setattr(sess, "_execute", execute)
    monkeypatch.setattr(sess, "emit", AsyncMock())
    monkeypatch.setattr(sess, "say", AsyncMock())
    await sess.stepup_resolved("approved", via="app", stepup_id="step-1")
    execute.assert_not_awaited()
    await sess.stepup_resolved("approved", via="passkey", stepup_id="unrelated")
    execute.assert_not_awaited()
    await sess.stepup_resolved("approved", via="passkey", stepup_id="step-1")
    execute.assert_awaited_once()


def test_historic_composite_score_cannot_compensate_for_bad_latest_speaker():
    state = TrustState()
    for seq in range(4):
        state.add(ChunkScore(seq, seq * 3, seq * 3 + 3, 0.70, 0.01))
    # Avoid a swap alarm: the current check is below the authorization threshold
    # but is not an explicit mismatch. The dashboard EMA still looks healthy.
    state.add(ChunkScore(5, 12, 15, 0.49, 0.01))
    d = decide(1, state, enrolled=True, voice_locked=False, pressure=0)
    assert d.outcome == "stepup"


def test_slow_response_is_not_clone_evidence():
    state = TrustState()
    d = decide(
        2,
        state,
        enrolled=True,
        voice_locked=False,
        pressure=0,
        liveness=LivenessOutcome(True, 30.0, 0.95, 0.01),
    )
    assert d.outcome == "allow"


@pytest.mark.asyncio
async def test_passkey_api_reports_execution_failure_without_debit(client, monkeypatch):
    from datetime import timedelta

    from app.core import session as sessions
    from app.core.transactions import freeze_transfer
    from app.db import session_scope
    from app.models import Account, ActionRequest, StepUp, utcnow
    from tests.passkey_device import provision
    from tests.test_transfer_security import proposal

    device = await provision(client)
    cid, aid, evidence = await proposal()
    params = {"payee_name": "Amma", "amount_rupees": 100}
    await freeze_transfer(aid, "cus_ananya", cid, params, evidence)
    async with session_scope() as db:
        account = await db.get(Account, "acc_ananya")
        account.balance_paise = 1
        st = StepUp(
            call_id=cid,
            action_id=aid,
            customer_id="cus_ananya",
            channel="registered_app",
            code_hash="",
            status="pending",
            expires_at=utcnow() + timedelta(seconds=120),
        )
        db.add(st)
        await db.flush()
        sid = st.id
    sess = CallSession(cid, customer_id="cus_ananya")
    sess.pending = Pending(
        aid, "transfer_funds", params, 2, stage="stepup", stepup_id=sid
    )
    monkeypatch.setattr(sess, "say", AsyncMock())
    monkeypatch.setattr(sess, "emit", AsyncMock())
    monkeypatch.setattr(
        sessions, "get_session", lambda call_id: sess if call_id == cid else None
    )
    app = {"X-App-Token": "app-ananya-demo"}
    response = await client.post(
        "/api/app/passkeys/options",
        headers=app,
        json={"purpose": "transfer", "stepup_id": sid},
    )
    assert response.status_code == 200, response.text
    options = response.json()
    response = await client.post(
        "/api/app/passkeys/verify",
        headers=app,
        json={
            "approval_id": options["approval_id"],
            "credential": device.authenticate(options["options"]),
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["status"] == "failed"
    async with session_scope() as db:
        assert (await db.get(Account, "acc_ananya")).balance_paise == 1
        assert (await db.get(ActionRequest, aid)).status == "failed"
