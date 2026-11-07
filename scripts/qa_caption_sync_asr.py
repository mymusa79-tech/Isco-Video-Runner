#!/usr/bin/env python3
"""Optional QA: compare burned caption cards with what a speech model hears in the final video.

Diagnostic only. It never edits a video, never feeds a gate and adds no provider call to a
production run. Word times come from an offline Whisper model (faster-whisper) run on the
GitHub runner; they are measured evidence, unlike the proportional estimates the captions use.
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import statistics
import sys
from pathlib import Path
from typing import Any, Sequence

_DIACRITICS = re.compile("[ً-ْٰـ]")
_NON_WORD = re.compile(r"[^\w]", re.UNICODE)
LATE_LEAD_THRESHOLD_SECONDS = 0.35
MIN_MATCH_RATIO = 0.6


def normalize(word: str) -> str:
    text = _DIACRITICS.sub("", str(word))
    text = _NON_WORD.sub("", text)
    for src, dst in (("أ", "ا"), ("إ", "ا"), ("آ", "ا"), ("ى", "ي"), ("ة", "ه")):
        text = text.replace(src, dst)
    return text


def card_words(cards: Sequence[dict[str, Any]]) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for index, card in enumerate(cards):
        for word in str(card.get("text") or "").split():
            if normalize(word):
                out.append((normalize(word), index))
    return out


def compare(cards: Sequence[dict[str, Any]], asr_words: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Per-card start/end error. Positive error = the card is late; negative = it is early."""
    script = card_words(cards)
    heard = [(normalize(w["word"]), float(w["start"]), float(w["end"])) for w in asr_words if normalize(w["word"])]
    if not script or not heard:
        return {"status": "no_data", "cards": [], "match_ratio": 0.0}
    matcher = difflib.SequenceMatcher(None, [w for w, _i in script], [w for w, _s, _e in heard], autojunk=False)
    matched: dict[int, tuple[float, float]] = {}
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            _w, _s, _e = heard[block.b + k]
            matched[block.a + k] = (_s, _e)
    ratio = len(matched) / len(script)
    rows: list[dict[str, Any]] = []
    for index, card in enumerate(cards):
        positions = [p for p, (_w, ci) in enumerate(script) if ci == index and p in matched]
        if not positions:
            rows.append({"card": index, "text": card.get("text"), "matched": False})
            continue
        first, last = matched[positions[0]], matched[positions[-1]]
        start_error = float(card["start"]) - first[0]
        end_error = float(card["end"]) - last[1]
        rows.append({
            "card": index, "text": card.get("text"), "matched": True,
            "card_start": round(float(card["start"]), 2), "heard_first_word_start": round(first[0], 2),
            "start_error": round(start_error, 2),
            "card_end": round(float(card["end"]), 2), "heard_last_word_end": round(last[1], 2),
            "end_error": round(end_error, 2),
            "flag": "early" if start_error < -LATE_LEAD_THRESHOLD_SECONDS
            else "late" if start_error > LATE_LEAD_THRESHOLD_SECONDS else "ok",
        })
    errors = [r["start_error"] for r in rows if r.get("matched")]
    status = "insufficient_match" if ratio < MIN_MATCH_RATIO or not errors else "measured"
    summary: dict[str, Any] = {"status": status, "match_ratio": round(ratio, 3), "cards": rows}
    if errors:
        summary.update({
            "median_start_error": round(statistics.median(errors), 2),
            "max_abs_start_error": round(max(abs(e) for e in errors), 2),
            "flagged_cards": sum(1 for r in rows if r.get("flag") in {"early", "late"}),
        })
    return summary


def load_cards(folder: Path) -> list[dict[str, Any]]:
    report = next(iter(sorted(folder.rglob("short-timed-text.json"))), None)
    if report is not None:
        data = json.loads(report.read_text(encoding="utf-8"))
        events = data.get("events") if isinstance(data, dict) else None
        if isinstance(events, list) and events:
            return [e for e in events if isinstance(e, dict)]
    srt = next(iter(sorted(folder.rglob("short-timed-text.srt"))), None)
    if srt is None:
        raise SystemExit("no short-timed-text.json or .srt found in the downloaded artifact")
    cards: list[dict[str, Any]] = []
    for block in re.split(r"\n\s*\n", srt.read_text(encoding="utf-8").strip()):
        lines = block.strip().splitlines()
        match = re.match(r"(\d+):(\d+):(\d+),(\d+)\s*-->\s*(\d+):(\d+):(\d+),(\d+)", lines[1]) if len(lines) >= 3 else None
        if match:
            a = [int(x) for x in match.groups()]
            cards.append({"start": a[0] * 3600 + a[1] * 60 + a[2] + a[3] / 1000,
                          "end": a[4] * 3600 + a[5] * 60 + a[6] + a[7] / 1000,
                          "text": " ".join(lines[2:])})
    return cards


def transcribe(video: Path, model_name: str) -> list[dict[str, Any]]:
    from faster_whisper import WhisperModel  # imported lazily: not needed for the offline tests

    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(str(video), language="ar", word_timestamps=True)
    return [{"word": w.word, "start": w.start, "end": w.end} for s in segments for w in (s.words or [])]


def to_markdown(report: dict[str, Any]) -> str:
    lines = [f"status: **{report['status']}**  match_ratio: {report['match_ratio']}",
             f"median start error: {report.get('median_start_error')}s  max: {report.get('max_abs_start_error')}s  flagged: {report.get('flagged_cards')}",
             "", "| card | text | card start | heard start | error | flag |", "|---|---|---|---|---|---|"]
    for row in report["cards"]:
        if row.get("matched"):
            lines.append(f"| {row['card']} | {row['text']} | {row['card_start']} | {row['heard_first_word_start']} | {row['start_error']} | {row['flag']} |")
        else:
            lines.append(f"| {row['card']} | {row['text']} | - | - | - | unmatched |")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--model", default="medium")
    args = parser.parse_args(argv)
    video = next(iter(sorted(args.artifact_dir.rglob("*.mp4"))), None)
    if video is None:
        raise SystemExit("no .mp4 found in the downloaded artifact")
    report = compare(load_cards(args.artifact_dir), transcribe(video, args.model))
    report["video"] = video.name
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "caption_sync_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (args.out / "caption_sync_report.md").write_text(to_markdown(report), encoding="utf-8")
    sys.stdout.write(to_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
