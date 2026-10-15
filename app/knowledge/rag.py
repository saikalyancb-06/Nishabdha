"""Extractive reviewed-source lookup. No generation or live web research on calls."""

import re

from app.knowledge import seed_data, store

STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "what",
    "how",
    "does",
    "can",
    "are",
    "why",
    "bank",
    "demo",
    "demonstration",
}


def verify_citation(sentence, evidence_text):
    """Only an exact excerpt is accepted; this checks provenance, not factual truth."""
    return (
        "supported"
        if sentence.strip() and sentence.strip() in evidence_text
        else "unsupported"
    )


async def retrieve_evidence(question, top_claims=10, top_passages=4):
    terms = {
        w.lower()
        for w in re.findall(r"\b\w{3,}\b", question)
        if w.lower() not in STOPWORDS
    }
    results = []
    for passage in store.search_chunks(question, k=20):
        words = set(re.findall(r"\b\w{3,}\b", passage["text"].lower()))
        overlap = len(terms & words)
        if overlap < min(1, len(terms)) or not terms:
            continue
        results.append(
            dict(
                passage,
                kind="passage",
                relevance=overlap,
                provenance="reviewed excerpt",
            )
        )
    return sorted(results, key=lambda p: p["relevance"], reverse=True)[:top_passages]


def is_evidence_adequate(question, ev):
    return bool(ev)


async def answer_rag(question, auto_research=False):
    # Compatibility argument intentionally cannot enable web access from a call.
    seed_data.seed_knowledge_base()
    evidence = await retrieve_evidence(question)
    accepted = evidence[:3]
    answer = [
        {
            "id": f"a{i+1}",
            "text": e["text"],
            "cites": [e["id"]],
            "source_info": f"{e['title']} ({e['url']})",
            "ruling": "supported",
        }
        for i, e in enumerate(accepted)
    ]
    summary = (
        answer[0]["text"]
        if answer
        else "I do not have a reviewed source that answers that question. Please contact the bank's verified support channel."
    )
    return {
        "question": question,
        "answer": answer,
        "spoken_summary": summary,
        "evidence": evidence,
        "covered": bool(answer),
        "verification": {
            "supported": len(answer),
            "partial": 0,
            "unsupported": 0,
            "no citation": 0,
        },
        "retrieved_from": "reviewed_excerpts",
        "researched": False,
        "run_id": None,
        "verification_note": "Exact excerpt provenance only; not a guarantee of factual correctness or complete coverage.",
    }
