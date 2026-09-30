from __future__ import annotations

"""Zero-cost audience-reality signals for Topic Research.

This module deliberately does NOT access Reddit or any community site. Reddit's current
terms require approved access for automated/commercial data use. Instead, the existing
single Research model call is asked to distill bounded lived-experience hypotheses from
the research evidence it already receives. If an approved external source later supplies
explicit [Reddit ...] evidence lines, the same parser can carry them downstream without
changing the Production contract.

No provider call, API key, dependency, workflow, retry, or failure authority is added.
"""

import re
from typing import Any, Iterable

SIGNAL_KINDS = ("pain", "situation", "question", "visual")
MAX_SIGNALS_PER_KIND = 2
MAX_SIGNAL_CHARS = 240

_PREFIX_RE = re.compile(
    r"^\[(?P<source>Audience|Reddit)\s+(?P<kind>pain|situation|question|visual)\]\s*(?P<text>.+)$",
    re.IGNORECASE,
)

PROMPT_SUFFIX = """
AUDIENCE REALITY SIGNALS (zero-extra-call contract):
Use only the RESEARCH_DATA already provided in this prompt. Do not browse, scrape, invent citations,
or claim that Reddit/community users said something unless such evidence is explicitly present in
RESEARCH_DATA. For each candidate, use the existing evidence string list to preserve a few concise,
paraphrased lived-experience hypotheses when they are genuinely supported:
- [Audience pain] the concrete recurring frustration/problem
- [Audience situation] an ordinary real-world situation where it appears
- [Audience question] the natural question a viewer may actually ask
- [Audience visual] one directly observable, stock-searchable scene that could communicate it
Keep each line under 240 characters, never include usernames, direct quotes, private details, or claims
of prevalence. These are creative/lived-experience signals, NOT scientific or market evidence. They do
not raise evidence_quality and must never replace the separate measured YouTube market evidence.
Prefer at most one line of each kind per candidate so the existing evidence list stays compact.
""".strip()


def augment_research_prompt(prompt: str) -> str:
    base = str(prompt or "").rstrip()
    if "AUDIENCE REALITY SIGNALS (zero-extra-call contract):" in base:
        return base
    return base + "\n\n" + PROMPT_SUFFIX


def extract_signals(evidence: Iterable[object] | None) -> dict[str, list[dict[str, str]]]:
    result: dict[str, list[dict[str, str]]] = {kind: [] for kind in SIGNAL_KINDS}
    for raw in evidence or ():
        text = " ".join(str(raw or "").split()).strip()
        match = _PREFIX_RE.match(text)
        if not match:
            continue
        kind = match.group("kind").lower()
        value = match.group("text").strip()[:MAX_SIGNAL_CHARS]
        if not value:
            continue
        rows = result[kind]
        normalized = value.casefold()
        if any(item["text"].casefold() == normalized for item in rows):
            continue
        if len(rows) >= MAX_SIGNALS_PER_KIND:
            continue
        rows.append(
            {
                "source": match.group("source").lower(),
                "text": value,
            }
        )
    return {kind: rows for kind, rows in result.items() if rows}


def attach_signals(candidate: dict[str, Any]) -> dict[str, Any]:
    out = dict(candidate)
    signals = extract_signals(out.get("evidence") if isinstance(out.get("evidence"), list) else [])
    if signals:
        out["community_signals"] = signals
    else:
        out.pop("community_signals", None)
    return out


def detail_lines(candidate: dict[str, Any]) -> list[str]:
    signals = candidate.get("community_signals")
    if not isinstance(signals, dict):
        signals = extract_signals(candidate.get("evidence") if isinstance(candidate.get("evidence"), list) else [])
    labels = {
        "pain": "الألم",
        "situation": "الموقف",
        "question": "السؤال",
        "visual": "المشهد",
    }
    lines: list[str] = []
    for kind in SIGNAL_KINDS:
        rows = signals.get(kind) if isinstance(signals, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows[:MAX_SIGNALS_PER_KIND]:
            if isinstance(row, dict):
                value = " ".join(str(row.get("text") or "").split()).strip()
                source = str(row.get("source") or "audience").lower()
            else:
                value = " ".join(str(row or "").split()).strip()
                source = "audience"
            if value:
                suffix = " · Reddit" if source == "reddit" else ""
                lines.append(f"• {labels[kind]}{suffix}: {value[:MAX_SIGNAL_CHARS]}")
    return lines[:8]
