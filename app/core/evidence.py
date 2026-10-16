"""Per-call evidence report (feature 8): 'prove who - or what - the caller is'.

Contains the decision trail, every voice-check score, flagged segments with clip references, the
waveform envelope with red ranges for the UI, and an integrity block (SHA-256 + HMAC) anchored to the
hash-chained audit log, so anyone can verify the report wasn't edited afterwards.
"""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

from sqlalchemy import select

from app.audio import decode_any, envelope
from app.config import get_settings
from app.core import audit
from app.db import session_scope
from app.models import (
    ActionRequest,
    Call,
    CallEvent,
    Challenge,
    Customer,
    StepUp,
    Ticket,
    VoiceCheck,
    utcnow,
)

REPORT_VERSION = "1.0"
_SKIP_EVENTS = {"agent_say_audio", "trust_update"}


def mask_phone(p: str | None) -> str:
    return f"******{p[-4:]}" if p and len(p) >= 4 else ""


def mask_name(n: str | None) -> str:
    if not n:
        return ""
    parts = n.split()
    return " ".join([parts[0]] + [x[0] + "." for x in parts[1:]])


def _sign(body: dict) -> dict:
    canon = audit.canonical(body)
    sha = hashlib.sha256(canon.encode()).hexdigest()
    sig = hmac.new(
        get_settings().audit_hmac_secret.encode(), sha.encode(), hashlib.sha256
    ).hexdigest()
    return {
        "sha256": sha,
        "hmac_sha256": sig,
        "algorithm": "sha256(canonical_json(report_without_integrity)) + HMAC",
    }


def verify_report(report: dict) -> dict:
    integ = report.get("integrity") or {}
    body = {k: v for k, v in report.items() if k != "integrity"}
    expect = _sign(body)
    ok = hmac.compare_digest(
        expect["sha256"], str(integ.get("sha256", ""))
    ) and hmac.compare_digest(expect["hmac_sha256"], str(integ.get("hmac_sha256", "")))
    return {"valid": ok, "sha256": expect["sha256"]}


def _verdict(
    call: Call, checks: list[VoiceCheck], actions: list[ActionRequest], meta: dict
) -> dict:
    flags = sorted({f for c in checks for f in (c.flags or [])})
    refused = [a for a in actions if a.status == "refused"]
    executed = [a for a in actions if a.status == "executed"]
    if "synthetic" in flags or "voice_swap" in flags:
        label = "SYNTHETIC / CLONED VOICE"
    elif refused:
        label = "NOT VERIFIED"
    elif "speaker_mismatch" in flags and not executed:
        label = "VOICE MISMATCH - NOT THE CUSTOMER"
    elif (
        checks
        and all(
            (c.speaker_prob or 0) >= 0.5 for c in checks if c.speaker_prob is not None
        )
        and (
            any(c.speaker_prob is not None for c in checks)
            or (executed and all((c.synthetic_prob or 1.0) < 0.3 for c in checks if c.synthetic_prob is not None))
        )
    ):
        label = "GENUINE CUSTOMER"
    else:
        label = "INCONCLUSIVE"
    reasons = []
    for a in refused:
        reasons += (a.decision or {}).get("reasons", [])
    return {
        "label": label,
        "risk_state": call.risk_state,
        "final_trust": round(call.trust or 0.0, 3),
        "flags": flags,
        "actions_executed": [a.intent for a in executed],
        "actions_refused": [a.intent for a in refused],
        "reasons": list(dict.fromkeys(reasons))[:10],
        "pressure": meta.get("pressure"),
        "pressure_cues": meta.get("pressure_cues", []),
    }


async def build_report(call_id: str) -> dict | None:
    async with session_scope() as db:
        call = await db.get(Call, call_id)
        if call is None:
            return None
        cust = await db.get(Customer, call.customer_id) if call.customer_id else None
        events = list(
            (
                await db.execute(
                    select(CallEvent)
                    .where(CallEvent.call_id == call_id)
                    .order_by(CallEvent.id)
                )
            ).scalars()
        )
        checks = list(
            (
                await db.execute(
                    select(VoiceCheck)
                    .where(VoiceCheck.call_id == call_id)
                    .order_by(VoiceCheck.seq)
                )
            ).scalars()
        )
        actions = list(
            (
                await db.execute(
                    select(ActionRequest)
                    .where(ActionRequest.call_id == call_id)
                    .order_by(ActionRequest.created_at)
                )
            ).scalars()
        )
        chals = list(
            (
                await db.execute(
                    select(Challenge)
                    .where(Challenge.call_id == call_id)
                    .order_by(Challenge.issued_at)
                )
            ).scalars()
        )
        steps = list(
            (
                await db.execute(
                    select(StepUp)
                    .where(StepUp.call_id == call_id)
                    .order_by(StepUp.created_at)
                )
            ).scalars()
        )
        tickets = list(
            (
                await db.execute(
                    select(Ticket)
                    .where(Ticket.call_id == call_id)
                    .order_by(Ticket.created_at)
                )
            ).scalars()
        )

    meta = call.meta or {}
    duration = ((call.ended_at or utcnow()) - call.started_at).total_seconds()
    wave, audio_dur = [], None
    rec = meta.get("recording")
    if rec and Path(rec).exists():
        try:
            x = decode_any(Path(rec).read_bytes())
            wave, audio_dur = envelope(x, 300), round(x.size / 16000, 2)
        except Exception:
            pass

    flagged = [
        {
            "seq": c.seq,
            "t_start": c.t_start,
            "t_end": c.t_end,
            "flags": c.flags,
            "speaker_prob": c.speaker_prob,
            "synthetic_prob": c.synthetic_prob,
            "clip": Path(c.clip_path).name if c.clip_path else "",
        }
        for c in checks
        if any(
            f in (c.flags or [])
            for f in (
                "synthetic",
                "synthetic_suspect",
                "speaker_mismatch",
                "voice_swap",
            )
        )
    ]
    swap_ev = next((e.payload for e in events if e.kind == "voice_swap"), None)
    body = {
        "report_version": REPORT_VERSION,
        "generated_at": utcnow().isoformat() + "Z",
        "call": {
            "id": call.id,
            "direction": call.direction,
            "channel": call.channel,
            "status": call.status,
            "outcome": call.outcome,
            "started_at": call.started_at.isoformat() + "Z",
            "ended_at": call.ended_at.isoformat() + "Z" if call.ended_at else None,
            "duration_s": round(duration, 1),
            "lang": meta.get("lang", call.lang),
            "caller_number": mask_phone(call.caller_number),
        },
        "customer": {
            "id": cust.id,
            "name": mask_name(cust.name),
            "phone": mask_phone(cust.phone),
            "voice_enrolled": cust.voice_enrolled,
        }
        if cust
        else None,
        "verdict": _verdict(call, checks, actions, meta),
        "voice_checks": [
            {
                "seq": c.seq,
                "purpose": c.purpose,
                "t_start": c.t_start,
                "t_end": c.t_end,
                "speaker_prob": c.speaker_prob,
                "synthetic_prob": c.synthetic_prob,
                "engines": {"speaker": c.speaker_source, "spoof": c.spoof_source},
                "latency_ms": c.latency_ms,
                "flags": c.flags,
            }
            for c in checks
        ],
        "flagged_segments": flagged,
        "voice_swap": swap_ev,
        "actions": [
            {
                "id": a.id,
                "intent": a.intent,
                "tier": a.tier,
                "status": a.status,
                "decision": a.decision,
                "result_summary": (a.result or {}).get("summary")
                or (a.result or {}).get("error"),
                "at": a.created_at.isoformat() + "Z",
            }
            for a in actions
        ],
        "liveness_challenges": [
            {
                "id": c.id,
                "action_id": c.action_id,
                "status": c.status,
                "attempts": c.attempts,
                "latency_s": c.latency_s,
                "heard": (c.details or {}).get("heard_digits"),
                "reasons": (c.details or {}).get("reasons", []),
            }
            for c in chals
        ],
        "step_ups": [
            {
                "id": s.id,
                "action_id": s.action_id,
                "channel": s.channel,
                "status": s.status,
                "attempts": s.attempts,
            }
            for s in steps
        ],
        "tickets": [
            {
                "id": t.id,
                "severity": t.severity,
                "status": t.status,
                "reasons": t.reasons,
                "clips": [Path(p).name for p in t.clip_paths],
            }
            for t in tickets
        ],
        "timeline": [
            {
                "t": e.t_call,
                "kind": e.kind,
                "data": {
                    k: v
                    for k, v in (e.payload or {}).items()
                    if k not in ("call_id", "audio_mp3_b64")
                },
            }
            for e in events
            if e.kind not in _SKIP_EVENTS
        ],
        "waveform": {
            "duration_s": audio_dur,
            "envelope": wave,
            "flagged_ranges": [
                {"t_start": f["t_start"], "t_end": f["t_end"], "flags": f["flags"]}
                for f in flagged
            ],
        },
        "audit": await audit.call_head(call_id),
    }
    from app.core.security_models import FrozenTransfer

    async with session_scope() as db:
        receipts = (
            await db.execute(
                select(FrozenTransfer).where(FrozenTransfer.call_id == call_id)
            )
        ).scalars()
        body["transaction_receipts"] = [r.receipt for r in receipts if r.receipt]
    body["assurance_limits"] = (
        "Signatures attest to this service's records, not perfect detection. Retain a signed checkpoint separately to detect audit-tail truncation."
    )
    return {**body, "integrity": _sign(body)}
