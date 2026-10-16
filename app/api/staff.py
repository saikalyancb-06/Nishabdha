"""Fraud desk dashboard, customer admin, enrollment, health and DetectifAI day-1 tools."""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audio import AudioError, assess, decode_any
from app.config import get_settings
from app.core import audit
from app.core.enrollment import PHRASES, EnrollmentError, enroll_customer, enrollment_info
from app.core.session import SESSIONS
from app.db import get_db
from app.events import DASHBOARD, hub
from app.models import Account, Call, Card, Customer, Ticket, VoiceEnrollment, utcnow
from app.providers.llm import get_llm
from app.providers.stt import get_stt
from app.providers.tts import get_tts
from app.providers.voice_engine import get_voice_engine
from app.security import _eq, is_staff, require_staff

router = APIRouter(prefix="/api", tags=["staff"])
STARTED = time.time()


# ------------------------------------------------------------------ health
@router.get("/health")
async def health():
    s = get_settings()
    eng = get_voice_engine()
    return {
        "status": "ok",
        "uptime_s": int(time.time() - STARTED),
        "live_calls": len(SESSIONS),
        "voice": eng.status(),
        "stt": {"provider": s.stt_provider, "fallback": s.stt_fallback, "groq_key": bool(s.effective_groq_key), "sarvam_key": bool(s.effective_sarvam_key), "breaker": get_stt().breaker.status()},
        "tts": {"provider": s.tts_provider, "breaker": get_tts().breaker.status()},
        "llm": {"provider": s.llm_provider, "model": get_llm().model, "available": get_llm().available, "breaker": get_llm().breaker.status()},
        "device": s.resolved_device,
    }


# ------------------------------------------------------------------ customers
class CustomerCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=120)
    phone: str = Field(..., pattern=r"^[6-9]\d{9}$")
    preferred_lang: str = Field("en", pattern="^(en|hi|kn)$")
    opening_balance_rupees: int = Field(10_000, ge=0, le=10**8)


@router.get("/customers", dependencies=[Depends(require_staff)])
async def list_customers(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(select(Customer).order_by(Customer.created_at))).scalars().all()
    return {"customers": [{"id": c.id, "name": c.name, "phone": c.phone, "lang": c.preferred_lang, "voice_enrolled": c.voice_enrolled, "voice_locked": c.voice_locked, "app_token": c.app_token} for c in rows]}


@router.post("/customers", dependencies=[Depends(require_staff)])
async def create_customer(body: CustomerCreate, db: AsyncSession = Depends(get_db)):
    if (await db.execute(select(Customer).where(Customer.phone == body.phone))).scalar_one_or_none():
        raise HTTPException(409, "phone already registered")
    import random

    c = Customer(name=body.name, phone=body.phone, preferred_lang=body.preferred_lang)
    db.add(c)
    await db.flush()
    db.add(Account(customer_id=c.id, number=f"501000{random.randint(10**7, 10**8 - 1)}", balance_paise=body.opening_balance_rupees * 100))
    db.add(Card(customer_id=c.id, last4=f"{random.randint(1000, 9999)}", kind="debit"))
    await db.commit()
    await audit.append("customer_created", {"customer_id": c.id})
    return {"id": c.id, "app_token": c.app_token}


@router.get("/customers/{customer_id}", dependencies=[Depends(require_staff)])
async def get_customer(customer_id: str, db: AsyncSession = Depends(get_db)):
    c = await db.get(Customer, customer_id)
    if c is None:
        raise HTTPException(404, "customer not found")
    return {
        "id": c.id, "name": c.name, "phone": c.phone, "lang": c.preferred_lang, "voice_enrolled": c.voice_enrolled, "voice_locked": c.voice_locked, "app_token": c.app_token,
        "accounts": [{"id": a.id, "number": a.number, "balance_paise": a.balance_paise} for a in c.accounts],
        "cards": [{"id": k.id, "last4": k.last4, "kind": k.kind, "status": k.status, "daily_limit_paise": k.daily_limit_paise, "international": k.international_enabled} for k in c.cards],
        "enrollment": await enrollment_info(c.id),
    }


@router.post("/demo/reset", dependencies=[Depends(require_staff)])
async def demo_reset():
    for sess in list(SESSIONS.values()):
        await sess.end("demo_reset")
    from app.seed import reset_demo

    get_voice_engine()._ref_cache.clear()
    return await reset_demo()


# ------------------------------------------------------------------ enrollment
@router.get("/enrollment/phrases")
async def phrases(lang: str = Query("en", pattern="^(en|hi|kn)$")):
    s = get_settings()
    return {"lang": lang, "phrases": PHRASES[lang], "min_samples": s.enroll_min_samples, "min_seconds_each": s.enroll_min_seconds}


@router.post("/customers/{customer_id}/enroll")
async def enroll(
    customer_id: str,
    files: list[UploadFile] = File(..., description="3-5 recordings of the customer reading the phrases"),
    x_staff_key: str | None = Header(None),
    x_app_token: str | None = Header(None),
    db: AsyncSession = Depends(get_db),
):
    """Enrollment is privileged: staff (branch/KYC) or the customer's own logged-in app only.
    Otherwise an attacker could register THEIR voice on a victim's account."""
    c = await db.get(Customer, customer_id)
    if c is None:
        raise HTTPException(404, "customer not found")
    actor = "staff" if is_staff(x_staff_key) else ("customer_app" if _eq(x_app_token, c.app_token) else None)
    if actor is None:
        raise HTTPException(401, "staff key or the customer's app token required")
    s = get_settings()
    if len(files) > 8:
        raise HTTPException(400, "at most 8 samples")
    raw = []
    for f in files:
        b = await f.read(s.max_upload_bytes + 1)
        if len(b) > s.max_upload_bytes:
            raise HTTPException(413, f"{f.filename} too large")
        raw.append(b)
    try:
        out = await enroll_customer(customer_id, raw, actor)
    except EnrollmentError as e:
        raise HTTPException(422, {"message": str(e), "details": e.details})
    await hub.publish(DASHBOARD, "voice_enrolled", {"customer_id": customer_id, "engines": out["engines"]})
    return out


@router.post("/audio/quality")
async def audio_quality(file: UploadFile = File(...)):
    """Check a recording before enrolling (UI can show 'too quiet' etc.)."""
    s = get_settings()
    b = await file.read(s.max_upload_bytes + 1)
    if len(b) > s.max_upload_bytes:
        raise HTTPException(413, "too large")
    try:
        x = await asyncio.to_thread(decode_any, b)
    except AudioError as e:
        raise HTTPException(400, str(e))
    return assess(x, s.enroll_min_seconds, s.enroll_min_rms_dbfs).as_dict()


@router.delete("/customers/{customer_id}/enrollment", dependencies=[Depends(require_staff)])
async def delete_enrollment(customer_id: str, db: AsyncSession = Depends(get_db)):
    c = await db.get(Customer, customer_id)
    if c is None:
        raise HTTPException(404, "customer not found")
    for e in (await db.execute(select(VoiceEnrollment).where(VoiceEnrollment.customer_id == customer_id))).scalars():
        e.active = False
    c.voice_enrolled = False
    await db.commit()
    get_voice_engine().forget(customer_id)
    await audit.append("voice_enrollment_removed", {"customer_id": customer_id})
    return {"ok": True}


# ------------------------------------------------------------------ fraud desk
@router.get("/dashboard/summary", dependencies=[Depends(require_staff)])
async def summary(db: AsyncSession = Depends(get_db)):
    since = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    total = (await db.execute(select(func.count()).select_from(Call).where(Call.started_at >= since))).scalar()
    fraud = (await db.execute(select(func.count()).select_from(Call).where(Call.started_at >= since, Call.risk_state == "fraud"))).scalar()
    open_t = (await db.execute(select(func.count()).select_from(Ticket).where(Ticket.status == "open"))).scalar()
    live = [
        {"call_id": cid, "customer_id": s.customer_id, "name": s.customer_name, "lang": s.lang, "direction": s.direction, **s.trust.snapshot(),
         "pressure": round(s.pressure.level, 2), "pending": None if not s.pending else {"intent": s.pending.intent, "stage": s.pending.stage, "tier": s.pending.tier}}
        for cid, s in SESSIONS.items()
    ]
    return {"today": {"calls": total, "fraud_calls": fraud}, "open_tickets": open_t, "live_calls": live}


@router.get("/tickets", dependencies=[Depends(require_staff)])
async def tickets(status: str | None = None, limit: int = Query(100, ge=1, le=500), db: AsyncSession = Depends(get_db)):
    q = select(Ticket).order_by(Ticket.created_at.desc()).limit(limit)
    if status:
        q = q.where(Ticket.status == status)
    rows = (await db.execute(q)).scalars().all()
    from pathlib import Path

    return {"tickets": [{"id": t.id, "call_id": t.call_id, "customer_id": t.customer_id, "action_id": t.action_id, "severity": t.severity, "status": t.status, "reasons": t.reasons,
                         "clips": [Path(p).name for p in t.clip_paths], "created_at": t.created_at.isoformat() + "Z", "resolution_note": t.resolution_note} for t in rows]}


class TicketUpdate(BaseModel):
    status: str = Field(..., pattern="^(open|confirmed_fraud|false_positive|closed)$")
    note: str = Field("", max_length=1000)
    analyst: str = Field("fraud-desk", max_length=80)
    lock_customer_voice: bool = False


@router.patch("/tickets/{ticket_id}", dependencies=[Depends(require_staff)])
async def update_ticket(ticket_id: str, body: TicketUpdate, db: AsyncSession = Depends(get_db)):
    t = await db.get(Ticket, ticket_id)
    if t is None:
        raise HTTPException(404, "ticket not found")
    t.status, t.resolution_note, t.resolved_by = body.status, body.note, body.analyst
    t.resolved_at = utcnow() if body.status != "open" else None
    if body.lock_customer_voice and t.customer_id:
        c = await db.get(Customer, t.customer_id)
        if c:
            c.voice_locked = True  # all voice-only sensitive actions now need branch/app
    await db.commit()
    if body.lock_customer_voice and t.customer_id:
        for sess in list(SESSIONS.values()):  # also lock calls that are live right now
            if sess.customer_id == t.customer_id:
                sess.voice_locked = True
                sess.trust.locked_reason = sess.trust.locked_reason or "customer voice locked by fraud desk"
    await audit.append("ticket_updated", {"ticket_id": t.id, "status": t.status, "analyst": body.analyst, "lock_voice": body.lock_customer_voice}, t.call_id)
    await hub.publish(DASHBOARD, "ticket_updated", {"ticket_id": t.id, "status": t.status})
    return {"id": t.id, "status": t.status}


@router.get("/audit/verify", dependencies=[Depends(require_staff)])
async def audit_verify():
    return await audit.verify_chain()


@router.get("/audit", dependencies=[Depends(require_staff)])
async def audit_list(call_id: str | None = None, limit: int = Query(200, ge=1, le=2000), db: AsyncSession = Depends(get_db)):
    from app.models import AuditEntry

    q = select(AuditEntry).order_by(AuditEntry.seq.desc()).limit(limit)
    if call_id:
        q = q.where(AuditEntry.call_id == call_id)
    rows = (await db.execute(q)).scalars().all()
    return {"entries": [{"seq": r.seq, "ts": r.ts.isoformat() + "Z", "call_id": r.call_id, "kind": r.kind, "payload": r.payload, "hash": r.hash, "prev_hash": r.prev_hash} for r in rows]}


# ------------------------------------------------------------------ admin / day-1 tools
@router.get("/admin/config", dependencies=[Depends(require_staff)])
async def admin_config():
    s = get_settings()
    hide = {
        "staff_api_key", "audit_hmac_secret", "groq_api_key", "groq_api_key1",
        "groq_api_key2", "groq_api_key3", "openai_api_key", "anthropic_api_key",
        "gemini_api_key", "detectifai_api_key", "sarvam_api_key", "sarvam_ai_api"
    }
    return {k: ("***" if k in hide and v else v) for k, v in s.model_dump(mode="json").items()}


@router.post("/admin/voice-check", dependencies=[Depends(require_staff)])
async def admin_voice_check(file: UploadFile = File(...), customer_id: str | None = Form(None), db: AsyncSession = Depends(get_db)):
    """Run one clip through the live engine chain (DetectifAI -> local) and show raw results.
    Use this on day 1 to confirm DetectifAI is wired correctly, and to demo a cloned clip to judges."""
    s = get_settings()
    b = await file.read(s.max_upload_bytes + 1)
    try:
        x = await asyncio.to_thread(decode_any, b)
    except AudioError as e:
        raise HTTPException(400, str(e))
    ref = None
    if customer_id:
        c = await db.get(Customer, customer_id)
        if c is None:
            raise HTTPException(404, "customer not found")
        enr = (await db.execute(select(VoiceEnrollment).where(VoiceEnrollment.customer_id == customer_id, VoiceEnrollment.active.is_(True)))).scalars().first()
        if enr:
            import numpy as np

            from app.providers.voice_engine import EnrollmentRef

            ref = EnrollmentRef(customer_id, enr.detectifai_ref, np.frombuffer(enr.local_embedding, np.float32) if enr.local_embedding else None,
                                np.array(enr.quality["mock_embedding"], np.float32) if enr.quality.get("mock_embedding") else None, list(enr.sample_paths or []))
    t0 = time.perf_counter()
    res = await get_voice_engine().check(ref, x)
    return {
        "duration_s": round(x.size / 16000, 2),
        "speaker": None if not res.speaker else {"prob": res.speaker.prob, "raw": res.speaker.raw, "engine": res.speaker.source, "latency_ms": res.speaker.latency_ms},
        "spoof": None if not res.spoof else {"synthetic_prob": res.spoof.synthetic_prob, "engine": res.spoof.source, "latency_ms": res.spoof.latency_ms, "detail": res.spoof.detail},
        "errors": res.errors,
        "total_ms": int((time.perf_counter() - t0) * 1000),
        "thresholds": {"synthetic_hard": s.synthetic_hard, "synthetic_soft": s.synthetic_soft, "speaker_reject": s.speaker_reject, "trust_allow": s.trust_allow},
    }


@router.post("/admin/calibrate-spoof", dependencies=[Depends(require_staff)])
async def calibrate_spoof(genuine: list[UploadFile] = File(..., description="Real voices recorded with the demo mic, in the demo room")):
    """Local anti-spoof models are sensitive to microphone/room. Record 5-10 genuine clips at the venue;
    this returns each model's raw P(fake) and a suggested LOCAL_SPOOF_THRESHOLD above the genuine scores."""
    eng = get_voice_engine()
    scores = []
    for f in genuine[:20]:
        try:
            x = await asyncio.to_thread(decode_any, await f.read(get_settings().max_upload_bytes))
            raw = await asyncio.to_thread(eng.local_spoof.raw_scores, x)
            scores.append({"file": f.filename, "models": raw, "mean": round(sum(raw.values()) / len(raw), 4)})
        except Exception as e:
            scores.append({"file": f.filename, "error": str(e)[:200]})
    means = [s["mean"] for s in scores if "mean" in s]
    suggestion = None if not means else round(min(0.9, max(0.3, max(means) + 0.1)), 2)
    return {"genuine_scores": scores, "max_genuine_mean": max(means) if means else None, "suggested_local_spoof_threshold": suggestion,
            "note": "Set LOCAL_SPOOF_THRESHOLD in .env to the suggestion and restart. Then test a cloned clip with /api/admin/voice-check."}
