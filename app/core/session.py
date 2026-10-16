"""CallSession: the live voice agent for one call.

Pipeline per call
  audio frames -> StreamSegmenter -> (a) 3 s speech chunks -> background voice checks -> TrustState
                                    -> (b) utterances      -> STT -> NLU -> policy gate -> action
Security-relevant speech (challenges, refusals, confirmations) always comes from fixed templates.
The LLM never executes anything; banking.execute() runs only after decide() returns 'allow' or an
approved step-up.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import numpy as np
from sqlalchemy import select

from app.audio import (
    SR,
    StreamSegmenter,
    Utterance,
    float_to_pcm16,
    pcm16_to_float,
    to_wav_bytes,
)
from app.config import get_settings
from app.core import audit
from app.core.banking import ActionError, execute
from app.core.liveness import evaluate_answer, new_challenge_digits
from app.core.localize import localize
from app.core.nlu import NLUResult, rule_params, understand
from app.core.policy import INTENTS, PARAM_PROMPTS, Decision, missing_params, tier_of
from app.core.pressure import PressureTracker
from app.core.stepup import check_code, code_matches, create_stepup, current_bank_call
from app.core.transactions import execute_transfer, freeze_transfer
from app.core.trust import ChunkScore, LivenessOutcome, TrustState, decide
from app.db import session_scope
from app.events import DASHBOARD, call_topic, customer_topic, hub
from app.i18n import extract_digits, speak_digits, t
from app.models import (
    ActionRequest,
    Call,
    CallEvent,
    Challenge,
    Customer,
    StepUp,
    Ticket,
    VoiceCheck,
    VoiceEnrollment,
    utcnow,
)
from app.providers.stt import get_stt
from app.providers.tts import get_tts
from app.providers.voice_engine import EnrollmentRef, VoiceCheckResult, get_voice_engine

log = logging.getLogger("unclone.session")

MAX_RECORDING_S = 20 * 60
AUDITED = {
    "call_started",
    "identified",
    "decision",
    "action_executed",
    "action_failed",
    "action_refused",
    "challenge_issued",
    "challenge_result",
    "stepup_sent",
    "stepup_result",
    "segment_flagged",
    "voice_swap",
    "ticket_created",
    "call_ended",
    "pressure_detected",
    "bank_verify_code",
    "protective_freeze",
}

Sender = Callable[[str, dict], Awaitable[None]]
# Only these reach the caller's own socket. Scores, flags, decisions and tickets go to the fraud desk only -
# telling an attacker WHY they were caught would help them tune their clone.
CALLER_EVENTS = {"agent_say", "transcript", "call_ended", "error", "action_executed"}


def describe_action(intent: str, p: dict) -> str:
    card = f"card ending {p['card_last4']}" if p.get("card_last4") else "your card"
    return {
        "card_unfreeze": f"unfreeze {card}",
        "card_block": f"permanently block {card}",
        "card_freeze": f"freeze {card}",
        "card_limit_change": f"change the daily limit on {card} to Rs {p.get('new_limit_rupees', '?')}",
        "card_international": f"turn international use {'on' if p.get('international') else 'off'} for {card}",
        "update_mobile": f"change your registered mobile to one ending {str(p.get('new_mobile', '????'))[-4:]}",
        "add_payee": f"add {p.get('payee_name', 'a payee')} as a payee",
        "transfer_funds": f"send Rs {p.get('amount_rupees', '?')} to {p.get('payee_name', 'the payee')}",
        "balance": "share your balance",
        "recent_transactions": "share your recent transactions",
        "card_status": "share your card status",
    }.get(intent, intent.replace("_", " "))


@dataclass
class Pending:
    action_id: str
    intent: str
    params: dict
    tier: int
    stage: str = "params"  # params | challenge | stepup
    challenge_id: str | None = None
    challenge_digits: str = ""
    challenge_attempts: int = 0
    prompt_msg_id: str | None = None
    prompt_sent_wall: float | None = None
    prompt_done_wall: float | None = None
    prompt_est_s: float = 0.0
    stepup_id: str | None = None
    liveness: LivenessOutcome | None = None
    awaiting_param: str | None = None
    request_evidence: dict = field(default_factory=dict)
    request_audio_hash: str = ""
    evidence_checked_at: float = 0.0
    evidence_status: str = "unknown"


@dataclass
class PlaybackInfo:
    sent_wall: float
    est_s: float
    done_wall: float | None = None


@dataclass
class CallSession:
    call_id: str
    lang: str = "en"
    direction: str = "inbound"
    customer_id: str | None = None
    customer_name: str = ""
    enrolled: bool = False
    voice_locked: bool = False
    ref: EnrollmentRef | None = None
    pending: Pending | None = None
    awaiting_identity_for: NLUResult | None = None
    ended: bool = False
    senders: list[Sender] = field(default_factory=list)
    greet_pending: bool = False
    greet_text: str = ""

    def __post_init__(self):
        s = get_settings()
        self.s = s
        self.trust = TrustState(s)
        self.pressure = PressureTracker()
        self.seg = StreamSegmenter(
            s.vad_aggressiveness,
            s.vad_end_silence_ms,
            s.vad_min_utterance_ms,
            s.vad_max_utterance_s,
            s.chunk_seconds,
            s.min_chunk_seconds,
        )
        self.t0_wall = time.monotonic()
        self._first_audio_wall: float | None = None
        self._rec = bytearray()
        self._turn_lock = asyncio.Lock()
        self._check_q: asyncio.Queue = asyncio.Queue(maxsize=6)
        self._worker: asyncio.Task | None = None
        self._turn_tasks: set[asyncio.Task] = set()
        self._seq = 0
        self._msg_seq = 0
        self._playback: dict[str, PlaybackInfo] = {}
        self._engines_ok = True
        self._pressure_warned = False
        self._ticketed_flags: set[str] = set()
        self.history: list[tuple[str, str]] = []
        self._checking = False
        self._transferred_paise = 0
        self.realtime = False  # True once live audio arrives over the WebSocket (timing is only meaningful then)
        self._frames_wall: list[
            tuple[int, float]
        ] = []  # (frame index at packet end, arrival wall)

    # ================================================================ plumbing
    def start(self):
        if self._worker is None:
            self._worker = asyncio.create_task(
                self._check_worker(), name=f"checks-{self.call_id}"
            )

    @property
    def t_call(self) -> float:
        return round(time.monotonic() - self.t0_wall, 3)

    async def emit(
        self, kind: str, data: dict, to_caller: bool = True, persist: bool = True
    ) -> None:
        data = {**data, "call_id": self.call_id, "t": self.t_call}
        dash = (
            {k: v for k, v in data.items() if k != "audio_mp3_b64"}
            if "audio_mp3_b64" in data
            else data
        )
        if persist:
            try:
                async with session_scope() as db:
                    db.add(
                        CallEvent(
                            call_id=self.call_id,
                            t_call=self.t_call,
                            kind=kind,
                            payload=_jsonable(data),
                        )
                    )
            except Exception as e:  # never let logging kill a call
                log.error("event persist failed: %s", e)
            if kind in AUDITED:
                try:
                    await audit.append(kind, data, self.call_id)
                except Exception as e:
                    log.error("audit append failed: %s", e)
        await hub.publish(DASHBOARD, kind, dash)
        await hub.publish(call_topic(self.call_id), kind, dash)
        if to_caller and kind in CALLER_EVENTS:
            for snd in list(self.senders):
                try:
                    await snd(kind, data)
                except Exception:
                    self.senders.remove(snd) if snd in self.senders else None

    async def say(
        self,
        text: str,
        kind: str = "normal",
        extra: dict | None = None,
        localize_dynamic: bool = False,
    ) -> str:
        if localize_dynamic and self.lang != "en":
            text = await localize(text, self.lang)
        sp = await get_tts().speak(text, self.lang)
        self._msg_seq += 1
        mid = f"m{self._msg_seq}"
        est = (
            (len(sp.audio_mp3) * 8 / 48_000)
            if sp.audio_mp3
            else max(1.5, len(text.split()) * 0.42)
        )
        self._playback[mid] = PlaybackInfo(time.monotonic(), est)
        self.history.append(("agent", text))
        await self.emit(
            "agent_say",
            {
                "id": mid,
                "text": text,
                "lang": self.lang,
                "kind": kind,
                "audio_mp3_b64": base64.b64encode(sp.audio_mp3).decode()
                if sp.audio_mp3
                else None,
                "client_tts": sp.client_tts,
                "voice": sp.voice,
                "est_duration_s": round(est, 2),
                **(extra or {}),
            },
            persist=False,
        )
        await self._persist_event("agent_say", {"id": mid, "text": text, "kind": kind})
        return mid

    async def _persist_event(self, kind: str, payload: dict):
        try:
            async with session_scope() as db:
                db.add(
                    CallEvent(
                        call_id=self.call_id,
                        t_call=self.t_call,
                        kind=kind,
                        payload=_jsonable(payload),
                    )
                )
        except Exception as e:
            log.error("event persist failed: %s", e)

    async def greet(self) -> None:
        if not self.greet_pending:
            return
        self.greet_pending = False
        await self.say(self.greet_text or t("greeting", self.lang), kind="greeting")

    def playback_event(self, msg_id: str, state: str) -> None:
        pb = self._playback.get(msg_id)
        if not pb:
            return
        if state == "ended" and pb.done_wall is None:
            pb.done_wall = time.monotonic()
        # The dispatch timestamp is immutable. Client playback telemetry must
        # never move the security timing baseline.

    # ================================================================ identity / enrollment
    async def load_customer(self, customer_id: str) -> bool:
        async with session_scope() as db:
            c = await db.get(Customer, customer_id)
            if c is None:
                return False
            enr = (
                await db.execute(
                    select(VoiceEnrollment)
                    .where(
                        VoiceEnrollment.customer_id == c.id,
                        VoiceEnrollment.active.is_(True),
                    )
                    .order_by(VoiceEnrollment.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            call = await db.get(Call, self.call_id)
            if call:
                call.customer_id = c.id
            self.customer_id, self.customer_name = c.id, c.name
            self.voice_locked = c.voice_locked
            self.enrolled = bool(c.voice_enrolled and enr)
            if enr:
                self.ref = EnrollmentRef(
                    customer_id=c.id,
                    detectifai_ref=enr.detectifai_ref,
                    local_embedding=np.frombuffer(enr.local_embedding, dtype=np.float32)
                    if enr.local_embedding
                    else None,
                    mock_embedding=np.array(
                        enr.quality.get("mock_embedding"), dtype=np.float32
                    )
                    if enr.quality.get("mock_embedding")
                    else None,
                    sample_paths=list(enr.sample_paths or []),
                )
        await self.emit(
            "identified",
            {
                "customer_id": self.customer_id,
                "name": self.customer_name,
                "enrolled": self.enrolled,
                "voice_locked": self.voice_locked,
            },
            to_caller=False,
        )
        return True

    async def _lookup_by_phone(self, text: str) -> bool:
        digits = extract_digits(text)
        if len(digits) < 10:
            return False
        async with session_scope() as db:
            # try every 10-digit window (speech often adds stray digits like "to" -> 2)
            cands = {digits[i : i + 10] for i in range(0, len(digits) - 9)}
            c = (
                (await db.execute(select(Customer).where(Customer.phone.in_(cands))))
                .scalars()
                .first()
            )
        return await self.load_customer(c.id) if c else False

    # ================================================================ audio in
    async def feed_pcm16(self, buf: bytes) -> None:
        if self.ended or not buf:
            return
        now = time.monotonic()
        self.realtime = True
        if self._first_audio_wall is None:
            self._first_audio_wall = now
        if len(self._rec) < MAX_RECORDING_S * SR * 2:
            self._rec.extend(buf[: MAX_RECORDING_S * SR * 2 - len(self._rec)])
        x = pcm16_to_float(buf)
        utts, chunks = self.seg.feed(x)
        self._note_wall(now)
        for audio, a, b in chunks:
            self._enqueue_check(audio, a, b)
        for u in utts:
            self._spawn_turn(u)

    def _note_wall(self, now: float):
        # Remember the wall-clock arrival of recent frames so utterance onsets can be mapped to wall time
        self._frames_wall.append((self.seg.frames_seen, now))
        self._frames_wall = self._frames_wall[-400:]

    def _onset_to_wall(self, onset_t: float) -> float | None:
        frame = int(onset_t * 1000 // 30)
        for idx, wall in self._frames_wall:
            if idx >= frame:
                return wall
        return None

    async def end_of_utterance(self) -> None:
        utts, chunks = self.seg.flush()
        for audio, a, b in chunks:
            self._enqueue_check(audio, a, b)
        for u in utts:
            self._spawn_turn(u)

    async def ingest_whole_utterance(
        self, x: np.ndarray, packet_s: float = 0.1
    ) -> None:
        """REST mode: the uploaded file IS one utterance (no VAD splitting on internal pauses).
        Audio still flows through the segmenter so continuous 3 s voice checks run exactly as live."""
        if self.ended or x.size == 0:
            return
        t_start = self.seg.now
        step = int(SR * packet_s)
        pcm = float_to_pcm16(x)
        if self._first_audio_wall is None:
            self._first_audio_wall = time.monotonic()
        for i in range(0, x.size, step):
            piece = pcm[i * 2 : (i + step) * 2]
            if len(self._rec) < MAX_RECORDING_S * SR * 2:
                self._rec.extend(piece[: MAX_RECORDING_S * SR * 2 - len(self._rec)])
            _, chunks = self.seg.feed(x[i : i + step])
            self._note_wall(time.monotonic())
            for audio, a, b in chunks:
                self._enqueue_check(audio, a, b)
        _, chunks = self.seg.flush()
        for audio, a, b in chunks:
            self._enqueue_check(audio, a, b)
        self.seg.reset_utterance()
        await self._safe_turn(Utterance(x, t_start, self.seg.now, t_start))

    def _spawn_turn(self, u: Utterance) -> None:
        task = asyncio.create_task(self._safe_turn(u))
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    async def _safe_turn(self, u: Utterance):
        try:
            await self.handle_utterance(u.audio, u.t_start, u.t_end, u.speech_onset)
        except Exception as e:
            log.exception("turn failed")
            await self.emit(
                "error", {"message": f"turn failed: {type(e).__name__}"}, persist=False
            )

    async def drain(self, timeout: float = 120.0) -> None:
        """Wait until queued checks and turns are processed (REST mode / tests)."""
        end = time.monotonic() + timeout
        while (
            self._turn_tasks
            or not self._check_q.empty()
            or getattr(self, "_checking", False)
        ) and time.monotonic() < end:
            await asyncio.sleep(0.05)

    # ================================================================ continuous voice checks
    def _enqueue_check(self, audio: np.ndarray, t_start: float, t_end: float) -> None:
        self._seq += 1
        item = (self._seq, audio, t_start, t_end)
        try:
            self._check_q.put_nowait(item)
        except asyncio.QueueFull:
            # CPU can't keep up: drop the OLDEST unchecked chunk (newest audio matters most) and record it
            try:
                dropped = self._check_q.get_nowait()
                asyncio.create_task(
                    self._persist_event("check_skipped", {"seq": dropped[0]})
                )
            except asyncio.QueueEmpty:
                pass
            self._check_q.put_nowait(item)

    async def _check_worker(self):
        while True:
            seq, audio, a, b = await self._check_q.get()
            self._checking = True
            try:
                await self.run_check(audio, a, b, purpose="stream", seq=seq)
            except Exception:
                log.exception("voice check failed")
            finally:
                self._checking = False

    async def run_check(
        self,
        audio: np.ndarray,
        t_start: float,
        t_end: float,
        purpose: str,
        seq: int | None = None,
    ) -> tuple[ChunkScore, VoiceCheckResult]:
        if seq is None:
            self._seq += 1
            seq = self._seq
        res = await get_voice_engine().check(self.ref if self.enrolled else None, audio)
        self._engines_ok = res.spoof is not None and (
            res.speaker is not None or not self.enrolled
        )
        cs = ChunkScore(
            seq,
            round(t_start, 2),
            round(t_end, 2),
            res.speaker_prob,
            res.synthetic_prob,
            purpose,
        )
        new_flags = self.trust.add(cs)
        clip = ""
        if new_flags or purpose == "challenge":
            clip = self._save_clip(audio, f"{seq}_{purpose}")
        async with session_scope() as db:
            db.add(
                VoiceCheck(
                    call_id=self.call_id,
                    seq=seq,
                    purpose=purpose,
                    t_start=t_start,
                    t_end=t_end,
                    speaker_prob=res.speaker_prob,
                    synthetic_prob=res.synthetic_prob,
                    speaker_source=res.speaker.source if res.speaker else "",
                    spoof_source=res.spoof.source if res.spoof else "",
                    latency_ms=max(
                        res.speaker.latency_ms if res.speaker else 0,
                        res.spoof.latency_ms if res.spoof else 0,
                    ),
                    flags=cs.flags,
                    raw={
                        "status": res.status,
                        "provenance": res.provenance,
                        "track": "caller",
                        "audio_hash": hashlib.sha256(float_to_pcm16(audio)).hexdigest(),
                        "errors": res.errors[:5],
                        "spoof": res.spoof.detail if res.spoof else None,
                        "speaker_raw": res.speaker.raw if res.speaker else None,
                    },
                    clip_path=clip,
                )
            )
            call = await db.get(Call, self.call_id)
            if call:
                call.trust = self.trust.trust
                call.risk_state = self.trust.risk_state
        await self.emit(
            "trust_update",
            {
                "chunk": {
                    "seq": seq,
                    "t_start": t_start,
                    "t_end": t_end,
                    "speaker_prob": res.speaker_prob,
                    "synthetic_prob": res.synthetic_prob,
                    "flags": cs.flags,
                    "purpose": purpose,
                    "engine": {
                        "speaker": res.speaker.source if res.speaker else None,
                        "spoof": res.spoof.source if res.spoof else None,
                    },
                    "errors": res.errors[:3],
                },
                **self.trust.snapshot(),
            },
            persist=False,
        )
        if new_flags:
            await self._on_flags(cs, new_flags, clip)
        return cs, res

    def _save_clip(self, audio: np.ndarray, name: str) -> str:
        try:
            p = self.s.data_dir / "clips" / f"{self.call_id}_{name}.wav"
            p.write_bytes(to_wav_bytes(audio))
            return str(p)
        except OSError as e:
            log.error("clip save failed: %s", e)
            return ""

    async def _on_flags(self, cs: ChunkScore, flags: list[str], clip: str) -> None:
        await self.emit(
            "segment_flagged",
            {
                "seq": cs.seq,
                "t_start": cs.t_start,
                "t_end": cs.t_end,
                "flags": flags,
                "speaker_prob": cs.speaker_prob,
                "synthetic_prob": cs.synthetic_prob,
                "clip": Path(clip).name if clip else "",
            },
            to_caller=False,
        )
        if "voice_swap" in flags and self.trust.swap:
            await self.emit("voice_swap", self.trust.swap.__dict__, to_caller=False)
        critical = [f for f in flags if f in ("synthetic", "voice_swap")]
        if critical and not (set(critical) <= self._ticketed_flags):
            self._ticketed_flags |= set(critical)
            await self.create_ticket(
                "critical",
                [self.trust.locked_reason or ", ".join(critical)],
                action_id=self.pending.action_id if self.pending else None,
                clips=[clip] if clip else [],
            )
            # A pending sensitive action is now dead: refuse it instead of waiting for the caller.
            # Scheduled (not awaited) because we may be inside a turn that already holds the turn lock.
            if self.pending is not None:
                asyncio.create_task(self._refuse_pending_if_locked())

    async def _refuse_pending_if_locked(self):
        async with self._turn_lock:
            p = self.pending
            if (
                p is None
                or self.ended
                or p.stage not in ("challenge", "stepup", "params", "evaluating")
            ):
                return
            d = decide(
                max(p.tier, 1),
                self.trust,
                enrolled=self.enrolled,
                voice_locked=self.voice_locked,
                pressure=self.pressure.level,
                liveness=p.liveness,
                engines_ok=self._engines_ok,
            )
            if d.outcome == "refuse":
                await self._record_decision(d)
                await self._refuse(d, ticket=False)

    # ================================================================ conversation
    async def handle_text(self, text: str) -> None:
        """Typed input (testing / accessibility). No audio -> no voice evidence from this turn."""
        async with self._turn_lock:
            if self.ended:
                return
            await self._turn(text, None, 0.0, 0.0, None)

    def spawn_text(self, text: str) -> None:
        """Tracked + exception-safe typed turn (used by the WebSocket)."""

        async def run():
            try:
                await self.handle_text(text)
            except Exception as e:
                log.exception("text turn failed")
                await self.emit(
                    "error",
                    {"message": f"turn failed: {type(e).__name__}"},
                    persist=False,
                )

        task = asyncio.create_task(run())
        self._turn_tasks.add(task)
        task.add_done_callback(self._turn_tasks.discard)

    async def handle_utterance(
        self, audio: np.ndarray, t_start: float, t_end: float, onset: float
    ) -> None:
        async with self._turn_lock:
            if self.ended:
                return
            onset_wall = self._onset_to_wall(onset)
            if self.pending and self.pending.stage == "challenge":
                # transcribe + voice-check the answer in parallel: the answer audio is bound to the nonce
                stt_task = asyncio.create_task(
                    get_stt().transcribe(audio, self.lang, digits=True)
                )
                check_task = asyncio.create_task(
                    self.run_check(audio, t_start, t_end, purpose="challenge")
                )
                try:
                    tr = await stt_task
                except Exception as e:
                    check_task.cancel()
                    await self.emit(
                        "error",
                        {"message": f"speech recognition failed: {e}"[:200]},
                        persist=False,
                    )
                    await self.say(t("need_more_audio", self.lang))
                    return
                cs, res = await check_task
                await self._transcript(tr.text, tr.lang, t_start, t_end, tr.source)
                if self.pending is None or self.pending.stage != "challenge":
                    return  # resolved meanwhile (e.g. synthetic flag -> refused)
                if _is_cancel(tr.text):
                    await self._cancel_pending()
                    return
                await self._answer_challenge(tr.text, res, onset_wall, audio)
                return
            try:
                tr = await get_stt().transcribe(audio, self.lang)
            except Exception as e:
                await self.emit(
                    "error",
                    {"message": f"speech recognition failed: {e}"[:200]},
                    persist=False,
                )
                await self.say(t("need_more_audio", self.lang))
                return
            await self._turn(tr.text, audio, t_start, t_end, tr.lang)

    async def _transcript(self, text: str, lang: str, a: float, b: float, source: str):
        self.history.append(("caller", text))
        await self.emit(
            "transcript",
            {"text": text, "lang": lang, "t_start": a, "t_end": b, "stt": source},
        )

    async def _turn(
        self,
        text: str,
        audio: np.ndarray | None,
        t_start: float,
        t_end: float,
        lang: str | None,
    ) -> None:
        if lang in ("en", "hi", "kn") and text.strip():
            self.lang = lang
        await self._transcript(
            text, self.lang, t_start, t_end, "audio" if audio is not None else "text"
        )
        if not text.strip():
            await self.say(t("need_more_audio", self.lang))
            return

        # ---- a previous evaluation crashed half-way: drop it rather than wedge the call
        if self.pending and self.pending.stage == "evaluating":
            await self._cancel_pending(silent=True)

        # ---- step-up in progress: caller may read the code
        if self.pending and self.pending.stage == "stepup":
            await self._stepup_turn(text)
            return

        # ---- typed answer to a voice challenge: no audio = no voice evidence -> fail closed (step-up)
        if self.pending and self.pending.stage == "challenge":
            if _is_cancel(text):
                await self._cancel_pending()
                return
            await self._answer_challenge(
                text,
                VoiceCheckResult(None, None, ["typed input - no audio to verify"]),
                None,
                None,
            )
            return

        ctx = " | ".join(f"{w}: {x}" for w, x in self.history[-6:])
        if self.pending and self.pending.stage == "params":
            ctx += f" | Agent is waiting for: {self.pending.awaiting_param} for action {self.pending.intent}"
        nlu = await understand(text, ctx)
        if nlu.lang and not lang:
            self.lang = nlu.lang
        level = self.pressure.update(nlu.pressure)
        if nlu.pressure.score >= 0.3:
            await self.emit(
                "pressure_detected",
                {
                    "score": round(nlu.pressure.score, 2),
                    "level": round(level, 2),
                    "cues": nlu.pressure.cues,
                },
                to_caller=False,
            )
        await self.emit(
            "nlu",
            {
                "intent": nlu.intent,
                "params": nlu.params,
                "source": nlu.source,
                "tier": tier_of(nlu.intent),
            },
            to_caller=False,
            persist=True,
        )
        if level >= 0.5 and not self._pressure_warned:
            self._pressure_warned = True
            await self.say(t("pressure_warning", self.lang), kind="warning")

        # ---- identity capture
        if self.awaiting_identity_for is not None:
            if await self._lookup_by_phone(text):
                pending_nlu, self.awaiting_identity_for = (
                    self.awaiting_identity_for,
                    None,
                )
                await self._post_identify(audio, t_start, t_end)
                await self.say(
                    t("identity_ok", self.lang, name=self.customer_name.split()[0])
                )
                await self._start_action(pending_nlu, audio, t_start, t_end)
            elif nlu.intent in ("goodbye", "confirm_no"):
                self.awaiting_identity_for = None
                await self._route(nlu, audio, t_start, t_end)
            else:
                await self.say(t("identity_not_found", self.lang))
            return

        # ---- collecting a missing parameter
        if self.pending and self.pending.stage == "params":
            if nlu.intent in ("confirm_no", "goodbye"):
                await self._cancel_pending()
                if nlu.intent == "goodbye":
                    await self._goodbye()
                return
            if (
                nlu.intent in INTENTS
                and INTENTS[nlu.intent].tier >= 1
                and nlu.intent != self.pending.intent
            ):
                await self._cancel_pending(silent=True)
                await self._start_action(nlu, audio, t_start, t_end)
                return
            extra = {**rule_params(self.pending.intent, text), **nlu.params}
            if (
                self.pending.awaiting_param == "card_last4"
                and "card_last4" not in extra
            ):
                d = extract_digits(text)
                if len(d) >= 4:
                    extra["card_last4"] = d[-4:]
            if (
                self.pending.awaiting_param == "new_mobile"
                and "new_mobile" not in extra
            ):
                d = extract_digits(text)
                if len(d) >= 10:
                    extra["new_mobile"] = d[-10:]
            if (
                self.pending.awaiting_param == "payee_account"
                and "payee_account" not in extra
            ):
                d = extract_digits(text)
                if 9 <= len(d) <= 18:
                    extra["payee_account"] = d
            if (
                self.pending.awaiting_param == "payee_name"
                and "payee_name" not in extra
                and nlu.intent in ("unknown", "provide_info")
            ):
                extra["payee_name"] = text.strip().strip(".").title()[:60]
            before = dict(self.pending.params)
            # only the parameter we asked for may overwrite; others fill gaps (e.g. "the one ending 8890"
            # must not be re-read as a new limit of Rs 8890)
            for k, v in extra.items():
                if k != "digits" and (
                    k == self.pending.awaiting_param or k not in self.pending.params
                ):
                    self.pending.params[k] = v
            if self.pending.params != before:
                self.pending.liveness = (
                    None  # a liveness pass only covers the parameters it was issued for
                )
                self.pending.request_evidence = {}
                self.pending.evidence_status = "unknown"
            await self._continue_action(audio, t_start, t_end)
            return

        await self._route(nlu, audio, t_start, t_end)

    async def _route(self, nlu: NLUResult, audio, t_start, t_end):
        it = nlu.intent
        if it == "goodbye":
            await self._goodbye()
        elif it == "verify_bank":
            await self._verify_bank()
        elif it in ("greeting", "faq"):
            if nlu.reply:
                await self.say(nlu.reply, localize_dynamic=nlu.source != "llm")
            else:
                await self.say(t("greeting", self.lang))
        elif it in ("unknown", "confirm_yes", "confirm_no", "provide_info"):
            if nlu.reply:
                await self.say(nlu.reply, localize_dynamic=nlu.source != "llm")
            else:
                await self.say(t("fallback", self.lang))
        else:
            await self._start_action(nlu, audio, t_start, t_end)

    async def _goodbye(self):
        await self.say(t("goodbye", self.lang), kind="goodbye")
        await self.end("caller_goodbye", lock_held=True)

    async def _verify_bank(self):
        if self.direction != "outbound":
            await self.say(
                "Open the bank app from your saved address to check your session. An inbound call cannot create proof of an outbound bank call."
            )
            return
        if not self.customer_id:
            self.awaiting_identity_for = NLUResult("verify_bank_identity")
            await self.say(t("ask_identity", self.lang))
            return
        async with session_scope() as db:
            n = await current_bank_call(db, self.customer_id)
            if n is None or n.call_id != self.call_id:
                await self.say(
                    "There is no active outbound verification for this session. Please check your bank app."
                )
                return
            code = n.code
            payload = {
                "notice_id": n.id,
                "code": n.code,
                "reason": n.reason,
                "expires_at": n.expires_at.isoformat(),
            }
        await hub.publish(
            customer_topic(self.customer_id), "bank_call_verification", payload
        )
        await self.emit(
            "bank_verify_code", {"notice_id": payload["notice_id"]}, to_caller=False
        )
        await self.say(
            t("bank_verify_sent", self.lang, code_spoken=speak_digits(code, self.lang))
        )

    async def _post_identify(self, audio, a, b):
        """Once we know who claims to be calling, score the identity utterance itself."""
        if (
            self.enrolled
            and audio is not None
            and audio.size >= SR * self.s.min_chunk_seconds
        ):
            await self.run_check(audio, a, b, purpose="identity")
        if self.customer_id:
            await hub.publish(
                customer_topic(self.customer_id),
                "call_connected",
                {
                    "call_id": self.call_id,
                    "message": "You are connected to Unclone Bank's verified phone line.",
                },
            )

    # ================================================================ actions
    async def _start_action(self, nlu: NLUResult, audio, a, b):
        intent = nlu.intent
        if intent == "verify_bank_identity":
            await self._verify_bank()
            return
        if intent not in INTENTS or INTENTS[intent].tier == 0:
            await self._route(nlu, audio, a, b)
            return
        if not self.customer_id:
            self.awaiting_identity_for = nlu
            await self.say(t("ask_identity", self.lang))
            return
        tier = tier_of(intent)
        async with session_scope() as db:
            act = ActionRequest(
                call_id=self.call_id,
                customer_id=self.customer_id,
                intent=intent,
                tier=tier,
                params=_jsonable(nlu.params),
            )
            db.add(act)
            await db.flush()
            aid = act.id
        self.pending = Pending(aid, intent, dict(nlu.params), tier)
        await self.emit(
            "action_requested",
            {"action_id": aid, "intent": intent, "tier": tier, "params": nlu.params},
            to_caller=False,
        )

        # Protective fast-path: a 'block my card' request freezes the card immediately (tier 1, reversible)
        if intent == "card_block":
            await self._protective_freeze(audio, a, b)
            if self.pending is None:
                return
        await self._continue_action(audio, a, b)

    async def _protective_freeze(self, audio, a, b):
        if not await self._ensure_evidence(audio, a, b):
            return
        d = decide(
            1,
            self.trust,
            enrolled=self.enrolled,
            voice_locked=self.voice_locked,
            pressure=self.pressure.level,
            engines_ok=self._engines_ok,
        )
        if d.outcome != "allow":
            return
        try:
            async with session_scope() as db:
                cust = await db.get(Customer, self.customer_id)
                r = await execute(db, cust, "card_freeze", self.pending.params)
        except ActionError:
            return  # ambiguity is handled by the main flow
        await self.emit(
            "protective_freeze",
            {"action_id": self.pending.action_id, "result": r},
            to_caller=False,
        )
        await self.say(
            r["summary"]
            + " To block it permanently and send a replacement, I need to verify it's you.",
            localize_dynamic=True,
        )

    async def _ensure_evidence(self, audio, a, b):
        """Check exactly this request's PCM, never substitute historic rolling trust."""
        p = self.pending
        if p is None:
            return False
        p.request_evidence = {}
        p.evidence_status = "unknown"
        p.evidence_checked_at = 0.0
        p.request_audio_hash = ""
        if (
            audio is None
            or audio.size < SR * self.s.min_chunk_seconds
        ):
            return False
        p.request_audio_hash = hashlib.sha256(float_to_pcm16(audio)).hexdigest()
        try:
            await asyncio.wait_for(
                self._drain_checks(), timeout=self.s.voice_timeout_s * 2
            )
            cs, res = await asyncio.wait_for(
                self.run_check(audio, a, b, purpose="request"),
                timeout=self.s.voice_timeout_s * 3,
            )
        except Exception as exc:
            p.request_evidence = {
                "status": "unknown",
                "reason": type(exc).__name__,
                "audio_hash": p.request_audio_hash,
            }
            return False
        p.evidence_checked_at = time.monotonic()
        p.evidence_status = res.provenance
        p.request_evidence = {
            "status": res.status,
            "provenance": res.provenance,
            "audio_hash": p.request_audio_hash,
            "track": "caller",
            "utterance_id": f"{self.call_id}:{a}:{b}:{p.request_audio_hash}",
            "call_id": self.call_id,
            "action_id": p.action_id,
            "seq": cs.seq,
            "t_start": a,
            "t_end": b,
            "checked_at": utcnow().isoformat(),
            "speaker_source": res.speaker.source if res.speaker else None,
            "spoof_source": res.spoof.source if res.spoof else None,
            "speaker_prob": res.speaker_prob,
            "synthetic_prob": res.synthetic_prob,
        }
        return self._request_evidence_valid(p)

    def _request_evidence_valid(self, p: Pending, *, transfer: bool = False) -> bool:
        if transfer and not self.enrolled:
            return False
        allowed = {"detectifai"}
        if self.s.app_env == "test":
            allowed.add("mock")
        if not transfer and self.s.app_env == "demo":
            allowed.add("mock")
        e = p.request_evidence
        sp, sy = e.get("speaker_prob"), e.get("synthetic_prob")
        speaker_ok = (
            (self.s.tier1_min_trust <= sp <= 1)
            if (self.enrolled and sp is not None)
            else True
        )
        return bool(
            p.evidence_status in allowed
            and e.get("audio_hash") == p.request_audio_hash
            and p.request_audio_hash
            and e.get("action_id") == p.action_id
            and e.get("call_id") == self.call_id
            and 0 <= time.monotonic() - p.evidence_checked_at <= 120
            and sy is not None
            and 0 <= sy < self.s.synthetic_soft
            and speaker_ok
        )

    async def _hold_pending(self, reason: str):
        p = self.pending
        if p is None:
            return
        p.stage = "held"
        async with session_scope() as db:
            act = await db.get(ActionRequest, p.action_id)
            if act:
                act.status = "held"
                act.decision = {
                    "outcome": "hold",
                    "reasons": [reason],
                    "evidence": p.request_evidence,
                }
        await self.emit(
            "action_held", {"action_id": p.action_id, "reason": reason}, to_caller=False
        )
        await self.say(
            "This request is on hold because current verification is incomplete. Please retry with your voice or use the bank app."
        )

    async def _continue_action(self, audio, a, b):
        p = self.pending
        if p is None:
            return
        miss = missing_params(p.intent, p.params)
        if miss:
            p.stage, p.awaiting_param = "params", miss[0]
            what = PARAM_PROMPTS.get(miss[0], {}).get(self.lang) or PARAM_PROMPTS.get(
                miss[0], {}
            ).get("en", miss[0])
            await self.say(t("missing_param", self.lang, what=what))
            return
        # Card ambiguity check before spending a challenge
        if p.intent.startswith("card_"):
            from app.core.banking import pick_card

            try:
                async with session_scope() as db:
                    card = await pick_card(db, self.customer_id, p.params)
                    p.params.setdefault("card_last4", card.last4)
            except ActionError as e:
                if e.need:
                    p.stage, p.awaiting_param = "params", e.need
                    await self.say(str(e), localize_dynamic=True)
                else:
                    await self._fail_action(str(e))
                return
        await self._evaluate(audio, a, b)

    async def _evaluate(self, audio=None, a=0.0, b=0.0):
        p = self.pending
        if p is None:
            return
        if p.tier >= 1:
            if p.liveness is None or audio is not None:
                await self._ensure_evidence(audio, a, b)
            await self._refresh_lock()
            if self.pending is not p:
                return
        d = decide(
            p.tier,
            self.trust,
            enrolled=self.enrolled,
            voice_locked=self.voice_locked,
            pressure=self.pressure.level,
            liveness=p.liveness,
            engines_ok=self._engines_ok,
        )
        if d.outcome != "refuse":
            if p.intent == "transfer_funds":
                if not self._request_evidence_valid(p, transfer=True):
                    await self._hold_pending(
                        "fresh DetectifAI request evidence required"
                    )
                    return
                d = Decision(
                    "stepup",
                    self.trust.trust,
                    ["transfer requires transaction-bound passkey approval"],
                )
            elif not self._request_evidence_valid(p):
                d = Decision(
                    "stepup",
                    self.trust.trust,
                    [
                        "current request has no complete approved voice evidence; app approval required"
                    ],
                )
        await self._record_decision(d)
        if d.outcome == "allow":
            await self._execute()
        elif d.outcome == "challenge":
            await self._issue_challenge()
        elif d.outcome == "stepup":
            await self._issue_stepup(d)
        else:
            await self._refuse(d)

    async def _refresh_lock(self):
        """The fraud desk can lock a customer's voice channel mid-call."""
        if not self.customer_id:
            return
        async with session_scope() as db:
            c = await db.get(Customer, self.customer_id)
            if c is not None and c.voice_locked:
                self.voice_locked = True

    async def _record_decision(self, d: Decision):
        p = self.pending
        if p is None:
            return
        async with session_scope() as db:
            act = await db.get(ActionRequest, p.action_id)
            if act:
                act.decision = _jsonable(d.as_dict())
                act.params = _jsonable(p.params)
        await self.emit(
            "decision",
            {
                "action_id": p.action_id,
                "intent": p.intent,
                "tier": p.tier,
                **d.as_dict(),
            },
        )

    async def _execute(self):
        p = self.pending
        if p is None or self.ended:
            return
        await self._refresh_lock()
        # last-moment guard: a flag may have landed between decision and execution
        if p.tier >= 1 and (
            self.trust.synthetic_flag is not None
            or self.trust.swap is not None
            or self.voice_locked
        ):
            await self._refuse(
                Decision(
                    "refuse",
                    self.trust.trust,
                    [self.trust.locked_reason or "voice channel locked"],
                )
            )
            return
        try:
            if p.intent == "transfer_funds":
                r = await execute_transfer(p.action_id, self.customer_id, self.call_id)
            else:
                async with session_scope() as db:
                    cust = await db.get(Customer, self.customer_id)
                    r = await execute(
                        db,
                        cust,
                        p.intent,
                        p.params,
                        call_transferred_paise=self._transferred_paise,
                    )
                    act = await db.get(ActionRequest, p.action_id)
                    act.status, act.result = "executed", _jsonable(r)
        except ActionError as e:
            if e.need:
                p.stage, p.awaiting_param = "params", e.need
                await self.say(str(e), localize_dynamic=True)
                return
            await self._fail_action(str(e))
            return
        if p.intent == "transfer_funds":
            self._transferred_paise += int(r.get("data", {}).get("amount_paise", 0))
        await self.emit(
            "action_executed",
            {"action_id": p.action_id, "intent": p.intent, "result": r},
        )
        if self.customer_id:
            await hub.publish(
                customer_topic(self.customer_id),
                "account_activity",
                {"intent": p.intent, "summary": r["summary"], "call_id": self.call_id},
            )
        self.pending = None
        await self.say(
            t("done", self.lang, result=await localize(r["summary"], self.lang)),
            kind="result",
        )

    async def _fail_action(self, msg: str):
        p = self.pending
        if p is None:
            return
        async with session_scope() as db:
            act = await db.get(ActionRequest, p.action_id)
            if act:
                act.status, act.result = "failed", {"error": msg}
        await self.emit(
            "action_failed",
            {"action_id": p.action_id, "intent": p.intent, "error": msg},
        )
        self.pending = None
        await self.say(msg, localize_dynamic=True)

    async def _cancel_pending(self, silent: bool = False):
        p = self.pending
        if not p:
            return
        async with session_scope() as db:
            act = await db.get(ActionRequest, p.action_id)
            if act and act.status not in ("executed", "refused"):
                act.status = "cancelled"
            if p.stepup_id:
                st = await db.get(StepUp, p.stepup_id)
                if st and st.status == "pending":
                    st.status = "expired"
        self.pending = None
        await self.emit("action_cancelled", {"action_id": p.action_id}, to_caller=False)
        if not silent:
            await self.say(t("cancelled", self.lang))

    # ---------------------------------------------------------------- liveness
    async def _issue_challenge(self, retry: bool = False):
        p = self.pending
        if p is None or self.ended:
            return
        digits = new_challenge_digits(self.s.challenge_digits, avoid=p.challenge_digits)
        async with session_scope() as db:
            from datetime import timedelta

            ch = Challenge(
                call_id=self.call_id,
                action_id=p.action_id,
                digits=digits,
                expires_at=utcnow() + timedelta(seconds=self.s.challenge_ttl_s),
                attempts=p.challenge_attempts,
            )
            db.add(ch)
            await db.flush()
            p.challenge_id = ch.id
            act = await db.get(ActionRequest, p.action_id)
            act.status = "awaiting_challenge"
        p.stage, p.challenge_digits = "challenge", digits
        spoken = speak_digits(digits, self.lang)
        prefix = (
            ""
            if retry
            else (
                await localize(
                    f"To {describe_action(p.intent, p.params)}, I need to confirm it's really you.",
                    self.lang,
                )
            )
            + " "
        )
        text = prefix + t(
            "challenge_retry" if retry else "challenge", self.lang, digits_spoken=spoken
        )
        await self.emit(
            "challenge_issued",
            {
                "action_id": p.action_id,
                "challenge_id": p.challenge_id,
                "attempt": p.challenge_attempts + 1,
                "digits_len": len(digits),
            },
            to_caller=False,
        )
        mid = await self.say(
            text, kind="challenge", extra={"challenge_id": p.challenge_id}
        )
        p.prompt_msg_id = mid

    async def _answer_challenge(
        self,
        text: str,
        res: VoiceCheckResult,
        onset_wall: float | None,
        audio: np.ndarray,
    ):
        p = self.pending
        if p is None:
            return
        pb = self._playback.get(p.prompt_msg_id or "")
        prompt_done = None
        if pb:
            # Server-side estimate always applies (a client that never reports playback can't switch timing
            # off). A client 'playback ended' report may only move it within [sent, estimate + 1 s].
            prompt_done = pb.sent_wall + pb.est_s
            # Playback acknowledgements are caller-controlled diagnostics only.
        measured = pb is not None and onset_wall is not None and self.realtime
        latency = (
            (onset_wall - prompt_done)
            if (measured and prompt_done is not None)
            else None
        )
        p.challenge_attempts += 1
        async with session_scope() as db:
            ch = await db.get(Challenge, p.challenge_id)
            expired = ch is not None and utcnow() > ch.expires_at
        ev = evaluate_answer(p.challenge_digits, text, latency, expired)
        outcome = LivenessOutcome(
            ev.passed, latency, res.speaker_prob, res.synthetic_prob, ev.reasons
        )
        async with session_scope() as db:
            ch = await db.get(Challenge, p.challenge_id)
            if ch:
                ch.status = (
                    "passed" if ev.passed else ("expired" if expired else "failed")
                )
                ch.transcript, ch.latency_s, ch.attempts = (
                    text[:500],
                    (round(latency, 3) if latency is not None else None),
                    p.challenge_attempts,
                )
                ch.details = _jsonable(
                    {
                        "heard_digits": ev.heard,
                        "similarity": ev.similarity,
                        "reasons": ev.reasons,
                        "latency_measured": measured,
                        "speaker_prob": res.speaker_prob,
                        "synthetic_prob": res.synthetic_prob,
                    }
                )
        await self.emit(
            "challenge_result",
            {
                "action_id": p.action_id,
                "challenge_id": p.challenge_id,
                "passed": ev.passed,
                "reasons": ev.reasons,
                "latency_s": None if latency is None else round(latency, 2),
                "latency_measured": measured,
                "speaker_prob": res.speaker_prob,
                "synthetic_prob": res.synthetic_prob,
            },
            to_caller=False,
        )
        if (
            not ev.passed
            and ev.retryable
            and p.challenge_attempts < self.s.challenge_max_attempts
        ):
            await self._issue_challenge(retry=True)
            return
        p.liveness = outcome
        p.stage = "evaluating"
        await self._evaluate()

    # ---------------------------------------------------------------- step-up
    async def _issue_stepup(self, d: Decision):
        p = self.pending
        if p is None or self.ended:
            return
        snapshot = None
        if p.intent == "transfer_funds":
            try:
                snapshot = await freeze_transfer(
                    p.action_id,
                    self.customer_id,
                    self.call_id,
                    p.params,
                    p.request_evidence,
                )
            except ActionError as exc:
                if exc.need:
                    p.stage, p.awaiting_param = "params", exc.need
                    await self.say(str(exc), localize_dynamic=True)
                else:
                    await self._fail_action(str(exc))
                return
        async with session_scope() as db:
            st, code = await create_stepup(
                db, self.call_id, p.action_id, self.customer_id
            )
            act = await db.get(ActionRequest, p.action_id)
            act.status = "awaiting_stepup"
            sid, exp = st.id, st.expires_at
        p.stage, p.stepup_id = "stepup", sid
        # Push ONLY to the registered app (old number). Never to a number mentioned on this call.
        await hub.publish(
            customer_topic(self.customer_id),
            "stepup_request",
            {
                "stepup_id": sid,
                "call_id": self.call_id,
                "action": describe_action(p.intent, p.params),
                "code": code,
                "expires_at": exp.isoformat(),
                "reasons": d.reasons,
                "transaction": snapshot,
                "requires_passkey": p.intent == "transfer_funds",
                "warning": "Approve only if YOU are on a call with Unclone Bank right now. Never share this code with anyone who called you.",
            },
        )
        await self.emit(
            "stepup_sent",
            {
                "action_id": p.action_id,
                "stepup_id": sid,
                "channel": "registered_app",
                "reasons": d.reasons,
            },
            to_caller=False,
        )
        await self.say(t("stepup", self.lang), kind="stepup", extra={"stepup_id": sid})

    async def _stepup_turn(self, text: str):
        nlu_no = text.strip().lower()
        if any(w in nlu_no for w in ("cancel", "रद्द", "ರದ್ದು")):
            await self._cancel_pending()
            return
        if self.pending is None:
            return
        spec = INTENTS.get(self.pending.intent)
        if spec and (spec.money_moving or self.pending.intent == "update_mobile"):
            # OTP-relay defence: for money movement / SIM-swap style changes the code can't be read out
            # on the call (a fraudster could be coaching the victim) - only the in-app Approve button counts.
            await self.say(
                await localize(
                    "For this request, please tap Approve in your bank app. I can't accept the code over the phone.",
                    self.lang,
                ),
                kind="stepup",
            )
            return
        digits = extract_digits(text)
        if len(digits) < 6:
            await self.say(t("stepup", self.lang), kind="stepup")
            return
        p = self.pending
        if (
            len(digits) > 10
        ):  # reciting long digit strings to fish for the code is not accepted
            digits = digits[:6]
        async with session_scope() as db:
            st = await db.get(StepUp, p.stepup_id)
            if st is None:
                return
            # speech may add a stray digit ("to" -> 2): test each 6-digit window, but count ONE attempt
            windows = [digits[i : i + 6] for i in range(0, len(digits) - 5)]
            hit = next((w for w in windows if code_matches(st, w)), None)
            check_code(st, hit or windows[0])
            status = st.status
        await self.stepup_resolved(status, via="voice_code")

    async def stepup_resolved(
        self, status: str, via: str, stepup_id: str | None = None
    ):
        """Called from voice code entry or from the customer's app (approve/deny)."""
        p = self.pending
        if not p or p.stage != "stepup" or self.ended:
            return
        if stepup_id is not None and stepup_id != p.stepup_id:
            return  # an old / unrelated approval can never release the current action
        await self.emit(
            "stepup_result",
            {
                "action_id": p.action_id,
                "stepup_id": p.stepup_id,
                "status": status,
                "via": via,
            },
        )
        if status == "approved":
            if p.intent == "transfer_funds" and (
                via != "passkey" or stepup_id != p.stepup_id
            ):
                await self.say(
                    "Approve the exact transaction with your passkey in the bank app.",
                    kind="stepup",
                )
                return
            await self._execute()
        elif status == "pending":  # wrong code, attempts left
            await self.say(t("stepup_wrong", self.lang))
        elif status == "denied":
            await self._refuse(
                Decision(
                    "refuse",
                    self.trust.trust,
                    [
                        "customer DENIED the request in their bank app - caller is not the customer"
                    ],
                )
            )
        else:
            await self._refuse(
                Decision("refuse", self.trust.trust, [f"step-up {status}"])
            )

    # ---------------------------------------------------------------- refusal
    async def _refuse(self, d: Decision, ticket: bool = True):
        p = self.pending
        if p is None:
            return
        async with session_scope() as db:
            act = await db.get(ActionRequest, p.action_id)
            if act:
                act.status = "refused"
                act.decision = _jsonable(d.as_dict())
            if p.stepup_id:
                st = await db.get(StepUp, p.stepup_id)
                if st and st.status == "pending":
                    st.status = "expired"  # the app must not show / accept an approval for a refused action
            call = await db.get(Call, self.call_id)
            if call:
                call.risk_state = "fraud"
        # No more sensitive actions on this call
        self.trust.locked_reason = self.trust.locked_reason or (
            "refused: " + (d.reasons[0] if d.reasons else "policy")
        )
        await self.emit(
            "action_refused",
            {"action_id": p.action_id, "intent": p.intent, "reasons": d.reasons},
        )
        if ticket:
            severity = (
                "critical"
                if any(
                    k in " ".join(d.reasons)
                    for k in ("synthetic", "changed mid-call", "DENIED")
                )
                else "high"
            )
            clips = [
                str(x)
                for x in sorted((self.s.data_dir / "clips").glob(f"{self.call_id}_*"))[
                    -5:
                ]
            ]
            await self.create_ticket(
                severity, d.reasons, action_id=p.action_id, clips=clips
            )
        if self.customer_id:
            await hub.publish(
                customer_topic(self.customer_id),
                "security_alert",
                {
                    "call_id": self.call_id,
                    "message": f"We stopped an attempt to {describe_action(p.intent, p.params)} on a phone call just now. Your account is safe. If this was you, our team will call you on your registered number.",
                },
            )
        self.pending = None
        await self.say(t("refuse", self.lang), kind="refusal")

    async def create_ticket(
        self, severity: str, reasons: list[str], action_id: str | None, clips: list[str]
    ) -> str:
        async with session_scope() as db:
            tk = Ticket(
                call_id=self.call_id,
                customer_id=self.customer_id,
                action_id=action_id,
                severity=severity,
                reasons=_jsonable(reasons),
                clip_paths=[c for c in clips if c],
            )
            db.add(tk)
            await db.flush()
            tid = tk.id
        await self.emit(
            "ticket_created",
            {
                "ticket_id": tid,
                "severity": severity,
                "reasons": reasons,
                "action_id": action_id,
            },
            to_caller=False,
        )
        return tid

    # ================================================================ end
    async def end(self, outcome: str = "ended", lock_held: bool = False) -> None:
        if self.ended:
            return
        if not lock_held:
            # wait (bounded) for an in-flight turn so we never cancel an action half-way through
            try:
                await asyncio.wait_for(self._turn_lock.acquire(), timeout=15)
            except asyncio.TimeoutError:
                pass
            else:
                try:
                    await self.end(outcome, lock_held=True)
                finally:
                    self._turn_lock.release()
                return
        if self.ended:
            return
        self.ended = True
        if self.pending and self.pending.stage in (
            "challenge",
            "stepup",
            "params",
            "evaluating",
        ):
            await self._cancel_pending(silent=True)
        if self._worker:
            # let in-flight checks finish briefly so the report is complete
            try:
                await asyncio.wait_for(self._drain_checks(), timeout=10)
            except asyncio.TimeoutError:
                pass
            self._worker.cancel()
        rec_path = ""
        if self._rec:
            rec_path = str(self.s.data_dir / "clips" / f"{self.call_id}_full.wav")
            try:
                Path(rec_path).write_bytes(
                    to_wav_bytes(pcm16_to_float(bytes(self._rec)))
                )
            except OSError:
                rec_path = ""
        async with session_scope() as db:
            call = await db.get(Call, self.call_id)
            if call:
                call.status, call.ended_at = "ended", utcnow()
                call.outcome = outcome
                call.trust, call.risk_state = self.trust.trust, self.trust.risk_state
                call.meta = {
                    **(call.meta or {}),
                    "recording": rec_path,
                    "lang": self.lang,
                    "pressure": round(self.pressure.level, 3),
                    "pressure_cues": self.pressure.cues,
                }
            if self.customer_id:
                from app.models import BankCallNotice

                for n in (
                    await db.execute(
                        select(BankCallNotice).where(
                            BankCallNotice.call_id == self.call_id,
                            BankCallNotice.status.in_(("active", "confirmed")),
                        )
                    )
                ).scalars():
                    n.status = "ended"
        await self.emit("call_ended", {"outcome": outcome, **self.trust.snapshot()})
        if self.customer_id:
            await hub.publish(
                customer_topic(self.customer_id),
                "call_ended",
                {"call_id": self.call_id},
            )
        SESSIONS.pop(self.call_id, None)

    async def _drain_checks(self):
        while not self._check_q.empty() or getattr(self, "_checking", False):
            await asyncio.sleep(0.05)


def _is_cancel(text: str) -> bool:
    import re

    return bool(
        re.search(
            r"\b(cancel|stop|never ?mind|forget it)\b|रद्द|ರದ್ದು|ಬೇಡ", text or "", re.I
        )
    )


def _jsonable(x):
    import json

    return json.loads(
        json.dumps(x, default=lambda o: o.tolist() if hasattr(o, "tolist") else str(o))
    )


SESSIONS: dict[str, CallSession] = {}


def get_session(call_id: str) -> CallSession | None:
    return SESSIONS.get(call_id)
