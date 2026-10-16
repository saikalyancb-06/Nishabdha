"""Diagnostic rolling score, mid-call swap detection, and explicit evidence gates.

Plain-language model:
  * every ~3 s of speech gets two numbers: "is this the enrolled customer?" (speaker_prob) and
    "is this a synthetic voice?" (synthetic_prob)
  * trust = smoothed speaker match x (1 - recent synthetic risk)
  * one strongly synthetic segment or a detected voice swap LOCKS the call for sensitive actions
  * tier-2 actions additionally need a fresh challenge answer whose own audio passes both checks
  * scores are provider signals, not claimed calibrated probabilities; rolling trust is diagnostic
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.config import Settings, get_settings
from app.core.policy import Decision


@dataclass
class ChunkScore:
    seq: int
    t_start: float
    t_end: float
    speaker_prob: float | None
    synthetic_prob: float | None
    purpose: str = "stream"
    flags: list[str] = field(default_factory=list)


@dataclass
class SwapEvent:
    at_seq: int
    t_start: float
    t_end: float
    baseline: float
    observed: float
    reason: str


class TrustState:
    def __init__(self, s: Settings | None = None):
        self.s = s or get_settings()
        self.chunks: list[ChunkScore] = []
        self.speaker_ema: float | None = None
        self.synthetic_flag: ChunkScore | None = None
        self.swap: SwapEvent | None = None
        self.locked_reason: str = ""
        self._cusum = 0.0
        self._baseline: list[float] = []

    # ------------------------------------------------------------ ingest
    def add(self, c: ChunkScore) -> list[str]:
        """Add a scored chunk; returns new flags raised by it."""
        s = self.s
        new_flags: list[str] = []
        if c.synthetic_prob is not None:
            if c.synthetic_prob >= s.synthetic_hard:
                new_flags.append("synthetic")
                if self.synthetic_flag is None:
                    self.synthetic_flag = c
                    self.locked_reason = (
                        self.locked_reason or "synthetic voice segment detected"
                    )
                    if len(self._baseline) >= self.s.swap_min_baseline_chunks:
                        self.note_synthetic_after_clean()
                        new_flags.append("voice_swap")
            elif c.synthetic_prob >= s.synthetic_soft:
                new_flags.append("synthetic_suspect")
        if c.speaker_prob is not None:
            if c.speaker_prob < s.speaker_reject:
                new_flags.append("speaker_mismatch")
            self.speaker_ema = (
                c.speaker_prob
                if self.speaker_ema is None
                else 0.6 * self.speaker_ema + 0.4 * c.speaker_prob
            )
            swap = self._swap_check(c)
            if swap:
                new_flags.append("voice_swap")
        new_flags = sorted(set(new_flags))
        c.flags = sorted(set(c.flags + new_flags))
        self.chunks.append(c)
        return new_flags

    def _swap_check(self, c: ChunkScore) -> SwapEvent | None:
        """Two-sided signal: the call started as a good match and then dropped sharply (CUSUM on the drop),
        or the synthetic score jumped after a clean start. Detects 'human starts, clone takes over'."""
        s = self.s
        p = c.speaker_prob
        if p is None:
            return None
        if len(self._baseline) < s.swap_min_baseline_chunks:
            if p >= 0.6 and (
                c.synthetic_prob is None or c.synthetic_prob < s.synthetic_soft
            ):
                self._baseline.append(p)
            return None
        base = sum(self._baseline) / len(self._baseline)
        drop = base - p
        self._cusum = max(
            0.0, self._cusum + drop - 0.1
        )  # 0.1 slack absorbs normal variation
        hard_drop = drop >= s.swap_drop and p < 0.5
        if self.swap is None and (hard_drop or self._cusum >= s.swap_drop * 1.5):
            self.swap = SwapEvent(
                c.seq,
                c.t_start,
                c.t_end,
                round(base, 3),
                round(p, 3),
                "speaker similarity dropped sharply mid-call",
            )
            self.locked_reason = self.locked_reason or "voice changed mid-call"
            return self.swap
        if p >= 0.6 and drop < 0.1:
            self._baseline = (self._baseline + [p])[-6:]
        return None

    def note_synthetic_after_clean(self) -> None:
        """Called by the session when a synthetic flag appears after a clean baseline - counts as a swap too."""
        if self.swap is None and self._baseline and self.synthetic_flag is not None:
            c = self.synthetic_flag
            self.swap = SwapEvent(
                c.seq,
                c.t_start,
                c.t_end,
                round(sum(self._baseline) / len(self._baseline), 3),
                c.speaker_prob or 0.0,
                "synthetic voice appeared after a genuine start",
            )

    # ------------------------------------------------------------ read
    @property
    def evidence_chunks(self) -> int:
        return sum(
            1
            for c in self.chunks
            if c.speaker_prob is not None or c.synthetic_prob is not None
        )

    @property
    def recent_synthetic(self) -> float:
        vals = [
            c.synthetic_prob for c in self.chunks[-3:] if c.synthetic_prob is not None
        ]
        return max(vals) if vals else 0.0

    @property
    def trust(self) -> float:
        if self.speaker_ema is None:
            t = (1.0 - self.recent_synthetic) if self.chunks else 0.5
        else:
            t = self.speaker_ema * (1.0 - self.recent_synthetic)
        if self.locked_reason:
            t = min(t, 0.1)
        return round(max(0.0, min(1.0, t)), 3)

    @property
    def risk_state(self) -> str:
        if self.locked_reason:
            return "fraud"
        if self.speaker_ema is None:
            return "unknown"
        if self.trust >= self.s.trust_allow:
            return "trusted"
        if self.trust >= self.s.trust_stepup:
            return "uncertain"
        return "suspicious"

    def snapshot(self) -> dict:
        return {
            "trust": self.trust,
            "speaker_ema": None
            if self.speaker_ema is None
            else round(self.speaker_ema, 3),
            "recent_synthetic": round(self.recent_synthetic, 3),
            "risk_state": self.risk_state,
            "locked": bool(self.locked_reason),
            "locked_reason": self.locked_reason,
            "chunks": len(self.chunks),
            "swap": None if not self.swap else self.swap.__dict__,
        }


@dataclass
class LivenessOutcome:
    passed: bool
    latency_s: float | None
    speaker_prob: float | None
    synthetic_prob: float | None
    reasons: list[str] = field(default_factory=list)


def decide(
    tier: int,
    state: TrustState,
    *,
    enrolled: bool,
    voice_locked: bool,
    pressure: float,
    liveness: LivenessOutcome | None = None,
    engines_ok: bool = True,
    s: Settings | None = None,
) -> Decision:
    """Pure function -> easy to unit test and to explain on the dashboard."""
    s = s or get_settings()
    trust = state.trust
    factors = {
        "tier": tier,
        "trust": trust,
        "speaker_ema": state.speaker_ema,
        "recent_synthetic": state.recent_synthetic,
        "pressure": round(pressure, 3),
        "enrolled": enrolled,
        "evidence_chunks": state.evidence_chunks,
        "liveness": None if liveness is None else liveness.__dict__,
        "engines_ok": engines_ok,
    }
    if tier <= 0:
        return Decision("allow", trust, ["no customer data involved"], factors)

    # ---------- hard refusals (any tier >= 1)
    hard: list[str] = []
    if voice_locked:
        hard.append("customer voice channel locked after confirmed fraud")
    if state.synthetic_flag is not None:
        hard.append(
            f"synthetic voice detected at {state.synthetic_flag.t_start:.1f}s (p={state.synthetic_flag.synthetic_prob:.2f})"
        )
    if state.swap is not None:
        hard.append(f"voice changed mid-call at {state.swap.t_start:.1f}s")
    if not hard and state.locked_reason:
        hard.append(f"call locked for sensitive actions ({state.locked_reason})")
    if hard:
        return Decision("refuse", trust, hard, factors)

    # ---------- tier 1: passive
    if tier == 1:
        if not engines_ok:
            return Decision(
                "stepup",
                trust,
                ["voice engines unavailable - fail closed to app approval"],
                factors,
            )
        if pressure >= 0.5:
            return Decision(
                "stepup", trust, ["pressure cues require app approval"], factors
            )
        if state.evidence_chunks < s.min_evidence_chunks:
            if (
                liveness is not None
            ):  # already asked once and still no usable voice (e.g. typed answer)
                return Decision(
                    "stepup",
                    trust,
                    ["no voice evidence after challenge - app approval required"],
                    factors,
                )
            return Decision(
                "challenge", trust, ["not enough speech yet to verify voice"], factors
            )
        last = state.chunks[-1] if state.chunks else None
        if last is not None and last.synthetic_prob is None:
            return Decision(
                "stepup",
                trust,
                ["voice evidence incomplete for latest audio - fail closed"],
                factors,
            )
        if enrolled and state.speaker_ema is not None and state.speaker_ema < s.speaker_reject:
            return Decision(
                "stepup", trust, ["voice does not match registered customer"], factors
            )
        if (
            last
            and (not enrolled or last.speaker_prob is None or last.speaker_prob >= s.tier1_min_trust)
            and last.synthetic_prob < s.synthetic_soft
        ):
            return Decision("allow", trust, ["passive voice check passed"], factors)
        return Decision(
            "stepup",
            trust,
            ["latest speaker or authenticity check requires app approval"],
            factors,
        )

    # ---------- tier 2: hard gate
    if not enrolled:
        return Decision(
            "stepup",
            trust,
            ["voice identity verification unavailable without reference - app approval required for sensitive action"],
            factors,
        )
    if liveness is None:
        return Decision(
            "challenge",
            trust,
            ["liveness challenge required for sensitive action"],
            factors,
        )
    reasons: list[str] = []
    if not liveness.passed:
        return Decision(
            "refuse", trust, ["liveness challenge failed"] + liveness.reasons, factors
        )
    sp, sy = liveness.speaker_prob, liveness.synthetic_prob
    if sy is not None and sy >= s.synthetic_hard:
        return Decision(
            "refuse",
            trust,
            [f"challenge answer sounded synthetic (p={sy:.2f})"],
            factors,
        )
    if enrolled and sp is not None and sp < s.speaker_reject:
        return Decision(
            "refuse",
            trust,
            [f"challenge answer voice does not match customer (p={sp:.2f})"],
            factors,
        )
    if sy is None:
        reasons.append(
            "voice engines could not score the challenge answer - fail closed"
        )
        return Decision("stepup", trust, reasons, factors)

    # Explicit evidence predicates authorize actions. The composite trust value
    # remains a dashboard diagnostic and cannot compensate for a failed check.
    if sy >= s.synthetic_soft:
        reasons.append("challenge authenticity check requires app approval")
    if enrolled and sp is not None and sp < s.tier1_min_trust:
        reasons.append("challenge speaker check requires app approval")
    if pressure >= 0.5:
        reasons.append("pressure cues require app approval")
    if not engines_ok:
        reasons.append("voice engines unavailable - app approval required")
    if reasons:
        return Decision("stepup", trust, reasons, factors)
    return Decision(
        "allow", trust, ["authenticity and liveness challenge checks passed"], factors
    )
