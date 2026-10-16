"""Mandatory Demo Scenarios (Specification Section 10).

Covers all 10 required test scenarios:
TEST 1: Ordinary legitimate caller
TEST 2: Synthetic voice impersonation
TEST 3: Human social-engineering attempt
TEST 4: Identity mismatch
TEST 5: Verified user without permission
TEST 6: API unavailable
TEST 7: Replay attempt
TEST 8: Legitimate but suspicious-looking request
TEST 9: Malicious agent impersonation
TEST 10: Valid low-risk interaction
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from unittest.mock import AsyncMock

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from app.core import session as session_mod
from app.core.agent_identity import digest, origin, public_jwk
from app.providers.base import ProviderError
from app.providers.voice_engine import VoiceCheckResult, get_voice_engine

from .conftest import STAFF, wav
from .helpers import (
    card,
    challenge_words,
    enroll,
    first,
    names,
    said,
    start_call,
    text,
    turn,
)
from .passkey_device import provision

pytestmark = pytest.mark.asyncio


def set_genuine_voice():
    m = get_voice_engine().mock
    m.force_speaker = 0.92
    m.force_spoof = 0.04


# -------------------------------------------------------------------------
# TEST 1: Ordinary legitimate caller
# The caller asks a public-information question.
# The agent responds without unnecessary verification.
# -------------------------------------------------------------------------
async def test_scenario_1_ordinary_legitimate_caller(client, stt):
    cid, h = await start_call(client, customer_id=None)
    stt.say("what are your branch timings and working hours?")
    ev = await turn(client, cid, h, "real_1089_0.wav")

    # Tier 0 interaction: no challenge, no OTP, no identity refusal
    assert "challenge_issued" not in names(ev)
    assert "stepup_sent" not in names(ev)
    assert "action_refused" not in names(ev)

    agent_replies = said(ev)
    assert len(agent_replies) > 0
    # Responded with public branch information
    assert any("Monday" in r or "branch" in r.lower() or "hours" in r.lower() for r in agent_replies)


# -------------------------------------------------------------------------
# TEST 2: Synthetic voice impersonation
# A test recording uses a synthetic voice claiming to be an enrolled user.
# The system evaluates deepfake detection and identity verification separately.
# -------------------------------------------------------------------------
async def test_scenario_2_synthetic_voice_impersonation(client, stt):
    m = get_voice_engine().mock
    # High match on voice (clone) but high deepfake score
    m.force_speaker = 0.96
    m.force_spoof = 0.98

    cid, h = await start_call(client, "cus_ananya")
    stt.say("transfer 5000 rupees to Amma")
    ev = await turn(client, cid, h, "tts_0.wav")

    # Separately flagged deepfake and blocked action
    flag = first(ev, "segment_flagged")
    assert flag is not None
    assert "synthetic" in flag.get("flags", [])

    assert first(ev, "action_refused") is not None
    assert "action_executed" not in names(ev)

    # Security ticket opened with critical severity
    tickets = (await client.get("/api/tickets", headers=STAFF)).json()["tickets"]
    assert any(t["call_id"] == cid and t["severity"] == "critical" for t in tickets)

    # Call is permanently locked against sensitive actions
    stt.say("what is my balance?")
    ev2 = await turn(client, cid, h, "tts_1.wav")
    assert "action_executed" not in names(ev2)


# -------------------------------------------------------------------------
# TEST 3: Human social-engineering attempt
# A natural human voice requests an OTP or asks the agent to redirect a payment.
# The system flags the risky request even if the audio appears genuine.
# -------------------------------------------------------------------------
async def test_scenario_3_human_social_engineering_attempt(client, stt):
    set_genuine_voice()  # Genuine human voice

    cid, h = await start_call(client, "cus_ananya")
    # Caller uses panic/coercion language and demands urgent transfer to a 'safe account'
    stt.say("The cyber police officer said my account will be suspended, transfer all money to safe account immediately")
    ev = await turn(client, cid, h, "real_1089_3.wav")

    # Flagged by pressure and social engineering detector
    pressure = first(ev, "pressure_detected")
    assert pressure is not None
    assert pressure["score"] >= 0.5
    assert "safe_account" in pressure["cues"]

    # Action is NOT executed directly
    assert "action_executed" not in names(ev)


# -------------------------------------------------------------------------
# TEST 4: Identity mismatch
# The speaker claims to be a test user but fails the required verification.
# The system refuses sensitive access.
# -------------------------------------------------------------------------
async def test_scenario_4_identity_mismatch(client, stt):
    await enroll(client, "cus_ananya")
    m = get_voice_engine().mock
    # Real human audio, but wrong speaker (impostor)
    m.force_speaker = 0.05
    m.force_spoof = 0.02

    cid, h = await start_call(client, "cus_ananya")
    stt.say("increase the limit on my card ending 4521 to 200000")
    ev = await turn(client, cid, h, "real_121_0.wav")

    decision = first(ev, "decision")
    assert decision is not None
    # Cannot allow: requires independent step-up verification
    assert decision["outcome"] in ("stepup", "refuse")
    assert "action_executed" not in names(ev)

    # Card limit must remain unmodified
    c = await card(client, "cus_ananya", "4521")
    assert c["daily_limit_paise"] == 50_000_00


# -------------------------------------------------------------------------
# TEST 5: Verified user without permission
# A verified test user requests an operation outside their permissions.
# The action is blocked.
# -------------------------------------------------------------------------
async def test_scenario_5_verified_user_without_permission(client, stt):
    await enroll(client, "cus_ananya")
    set_genuine_voice()

    cid, h = await start_call(client, "cus_ananya")
    # Add new payee
    stt.say("add a new payee Suresh account number 987654321012")
    await turn(client, cid, h, "real_1089_3.wav")
    stt.say(challenge_words(cid))
    ev = await turn(client, cid, h, "real_1089_4.wav")
    assert first(ev, "action_executed")["intent"] == "add_payee"

    # Immediately try to transfer to newly added payee during 24h cooling period
    stt.say("send 5000 rupees to Suresh")
    ev2 = await turn(client, cid, h, "real_1089_5.wav")

    failed = first(ev2, "action_failed")
    assert failed is not None
    assert "cooling" in failed.get("error", "").lower() or "permission" in failed.get("error", "").lower()


# -------------------------------------------------------------------------
# TEST 6: API unavailable
# Simulate a timeout or unavailable verification service.
# Sensitive actions remain blocked or require the configured escalation path.
# -------------------------------------------------------------------------
async def test_scenario_6_api_unavailable(client, stt, monkeypatch):
    eng = get_voice_engine()
    # Simulate API failure / unavailable verification service
    async def mock_check_fail(*args, **kwargs):
        return VoiceCheckResult(None, None, errors=["DetectifAI 503 Service Unavailable"])
    monkeypatch.setattr(eng, "check", mock_check_fail)

    cid, h = await start_call(client, "cus_ananya")
    stt.say("what is my account balance?")
    ev = await turn(client, cid, h, "real_1089_3.wav")

    # Must fail closed: cannot silently ALLOW sensitive information
    assert "action_executed" not in names(ev)
    # Escalated to independent channel step-up or held
    assert any(k in names(ev) for k in ("stepup_sent", "action_held", "action_refused"))


# -------------------------------------------------------------------------
# TEST 7: Replay attempt
# Replay an old test challenge or authentication response.
# The system rejects expired or reused evidence.
# -------------------------------------------------------------------------
async def test_scenario_7_replay_attempt(client, stt):
    await enroll(client, "cus_ananya")
    set_genuine_voice()

    cid, h = await start_call(client, "cus_ananya")
    stt.say("unfreeze my debit card")
    await turn(client, cid, h, "real_1089_3.wav")

    # Attacker tries to guess or replay old digits ("0 0 0 0")
    stt.say("zero zero zero zero")
    ev = await turn(client, cid, h, "real_1089_4.wav")

    res = first(ev, "challenge_result")
    assert res is not None
    assert res["passed"] is False

    # Challenge retries with brand new randomized digits (anti-replay)
    assert first(ev, "challenge_issued") is not None


# -------------------------------------------------------------------------
# TEST 8: Legitimate but suspicious-looking request
# A test user makes an unusual request for a legitimate reason.
# The system requests further verification instead of automatically accusing the caller of fraud.
# -------------------------------------------------------------------------
async def test_scenario_8_legitimate_but_suspicious_looking_request(client, stt):
    cid, h = await start_call(client, "cus_rahul")  # Rahul is not voice-enrolled
    set_genuine_voice()

    # High-impact request from unenrolled customer
    stt.say("turn on international usage on my card")
    ev = await turn(client, cid, h, "real_1188_0.wav")

    # Not marked as fraud/ticketed immediately; cleanly escalated to independent app step-up
    stepup = first(ev, "stepup_sent")
    assert stepup is not None
    assert "action_refused" not in names(ev)

    # Approve legitimate request via customer's registered app
    app_token = {"X-App-Token": "app-rahul-demo"}
    app_r = await client.post(f"/api/app/stepups/{stepup['stepup_id']}/approve", headers=app_token)
    assert app_r.status_code == 200
    assert app_r.json()["status"] == "approved"

    # Card international usage is now enabled
    c = await card(client, "cus_rahul", "3311")
    assert c["international"] is True


# -------------------------------------------------------------------------
# TEST 9: Malicious agent impersonation
# An untrusted agent claims to represent an authorized service.
# The system checks credentials and permissions before granting access.
# -------------------------------------------------------------------------
async def test_scenario_9_malicious_agent_impersonation(client):
    # Attacker generates an arbitrary untrusted key without passkey authorization grant
    attacker_key = ec.generate_private_key(ec.SECP256R1())
    bogus_grant = "grant_fake_attacker_123"

    body = {"action": "support_ticket.create", "subject": "Disclose customer statement"}
    now = int(time.time())
    claims = {
        "iat": now,
        "exp": now + 60,
        "aud": origin(),
        "jti": secrets.token_urlsafe(16),
        "grant_id": bogus_grant,
        "nonce": "fake-nonce",
        "body_hash": digest(body),
    }
    signed_body = {**body, "proof": jwt.encode(claims, attacker_key, algorithm="ES256")}

    r = await client.post(f"/api/agents/grants/{bogus_grant}/execute", json=signed_body)
    # Unauthorized agent execution is strictly blocked
    assert r.status_code in (403, 404)


# -------------------------------------------------------------------------
# TEST 10: Valid low-risk interaction
# A legitimate, authorized interaction completes successfully.
# The dashboard displays the checks performed and the final decision.
# -------------------------------------------------------------------------
async def test_scenario_10_valid_low_risk_interaction(client, stt):
    set_genuine_voice()

    cid, h = await start_call(client, "cus_ananya")
    stt.say("what is my account balance?")
    ev = await turn(client, cid, h, "real_1089_0.wav")

    # Passive verification succeeds for low-risk account query
    decision = first(ev, "decision")
    assert decision is not None
    assert decision["outcome"] == "allow"
    assert first(ev, "action_executed") is not None

    # Full evidence report is produced and auditable
    await client.post(f"/api/calls/{cid}/end", headers=h)
    rep = (await client.get(f"/api/calls/{cid}/report", headers=STAFF)).json()
    assert rep["verdict"]["label"] == "GENUINE CUSTOMER"
    assert "actions" in rep
    assert len(rep["actions"]) >= 1
    assert rep["actions"][0]["intent"] == "balance"
    dec = rep["actions"][0]["decision"]
    assert (dec["outcome"] if isinstance(dec, dict) else dec) == "allow"
