"""Social-engineering / pressure-tactic detection (feature 10).

Lexicon (en / hi / kn, native script + romanised) gives an instant, explainable score; the LLM adds a
contextual score. Final = max(lexicon, llm). High pressure raises the trust bar and forces step-up.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# (category, weight, patterns)
_CUES: list[tuple[str, float, list[str]]] = [
    ("urgency", 0.35, [
        r"\b(right now|immediately|urgent(ly)?|asap|hurry|quickly|within (\d+|five|ten|two) minutes|no time)\b",
        r"(turant|jaldi|abhi ke abhi|फ़ौरन|फौरन|तुरंत|जल्दी|अभी के अभी)",
        r"(bega|beega|takshana|ಬೇಗ|ತಕ್ಷಣ|ಈಗಲೇ)",
    ]),
    ("authority_threat", 0.4, [
        r"\b(police|cyber ?cell|cbi|rbi officer|security team|bank security|bank officer|income tax|arrest(ed)?|legal action|court|warrant|customs|ed officer|digital arrest)\b",
        r"(पुलिस|गिरफ्तार|गिरफ़्तार|कानूनी कार्रवाई|डिजिटल अरेस्ट)",
        r"(ಪೊಲೀಸ್|ಬಂಧನ|ಅರೆಸ್ಟ್)",
    ]),
    ("account_threat", 0.3, [
        r"\b(account (will be|is going to be|gets?|is) (blocked|frozen|closed|suspended|at risk|compromised)|account (is )?at risk|kyc (expired|pending|update)|last chance)\b",
        r"(खाता (बंद|ब्लॉक) हो जाएगा|केवाईसी)",
        r"(ಖಾತೆ (ಬ್ಲಾಕ್|ಮುಚ್ಚ))",
    ]),
    ("safe_account", 0.5, [
        r"\b(safe account|secure account|holding account|rbi account|protect (your|the) money by (moving|transferring))\b",
        r"(सुरक्षित खाते)",
        r"(ಸುರಕ್ಷಿತ ಖಾತೆ)",
    ]),
    ("secrecy", 0.35, [
        r"\b(don'?t tell (anyone|your family|the bank)|keep (this|it) (secret|confidential)|stay on the line)\b",
        r"(किसी को (मत|ना) बताना)",
        r"(ಯಾರಿಗೂ ಹೇಳಬೇಡಿ)",
    ]),
    ("otp_request", 0.4, [
        r"\b(share|tell|read|give)( me| us)? (the |your )?(otp|pin|cvv|password|code)\b",
        r"(ओटीपी बताओ|ओटीपी बताइए|पिन बताओ)",
        r"(ಒಟಿಪಿ ಹೇಳಿ)",
    ]),
    ("remote_access", 0.4, [
        r"\b(anydesk|teamviewer|quick ?support|screen ?share|install (this|an) app)\b",
    ]),
    ("third_party_coaching", 0.3, [
        r"\b(he|she|they|the officer|someone) (is )?(telling|asking) me to\b",
        r"\b(my (son|daughter|friend|boss) (said|asked|told))\b",
    ]),
]
_COMPILED = [(c, w, [re.compile(p, re.I) for p in ps]) for c, w, ps in _CUES]


@dataclass
class PressureResult:
    score: float
    cues: list[str] = field(default_factory=list)
    source: str = "lexicon"


def lexicon_score(text: str) -> PressureResult:
    if not text:
        return PressureResult(0.0)
    cues, score = [], 0.0
    for cat, w, pats in _COMPILED:
        if any(p.search(text) for p in pats):
            cues.append(cat)
            score += w
    return PressureResult(min(1.0, score), cues)


def combine(lex: PressureResult, llm_score: float | None, llm_cues: list[str] | None) -> PressureResult:
    llm_score = max(0.0, min(1.0, float(llm_score or 0.0)))
    cues = list(dict.fromkeys(lex.cues + [f"llm:{c}" for c in (llm_cues or [])][:5]))
    if llm_score > lex.score:
        return PressureResult(llm_score, cues, "llm+lexicon")
    return PressureResult(lex.score, cues, "lexicon" if not llm_cues else "llm+lexicon")


class PressureTracker:
    """Call-level pressure: decays slowly so one calm sentence doesn't reset it."""

    def __init__(self, decay: float = 0.85):
        self.level = 0.0
        self.decay = decay
        self.cues: list[str] = []

    def update(self, r: PressureResult) -> float:
        self.level = max(r.score, self.level * self.decay)
        for c in r.cues:
            if c not in self.cues:
                self.cues.append(c)
        return self.level
