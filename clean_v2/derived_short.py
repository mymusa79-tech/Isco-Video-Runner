"""Optional, offline extraction from a SUCCESS parent; never imports or calls TTS.

The first voice pass may expose approved sentence boundaries as measured chunks.
Extraction and retries only read the frozen video, audio and timing evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import zipfile
from pathlib import Path
from typing import Any, Mapping

from .contracts import atomic_write_json
from .production_package import section_text, spoken_text

INPUT_NAMES = ("final.mp4", "narration-mastered.wav", "timeline-first.json", "voice-sections.json",
               "script.json", "production.json", "derivative-context.json")
CAPTION_ROLES = {"topic", "outro", "promo_short", "cta_topic"}


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _prefix(root: Path) -> str:
    return "podcast-short" if _read(root / "timeline-first.json")["format"] == "podcast" else "long-short"


def split_voice_units(units: list[tuple[str, str]], candidate: Mapping[str, str] | None) -> list[tuple[str, str]]:
    """Expose complete-sentence anchors in the SAME first synthesis pass.

    Text is never generated here. When a split continues a dialogue turn, carry
    its existing speaker label so Gemini cannot assign that fragment another voice.
    """
    if not candidate:
        return units
    start, end = str(candidate.get("start_text") or ""), str(candidate.get("end_text") or "")
    if not start or not end or not all(re.search(r"[.!؟!]$", x.strip()) for x in (start, end)):
        return units
    result = []
    for role, text in units:
        cuts = {0, len(text)}
        if role in CAPTION_ROLES:
            if text.count(start) == 1:
                cuts.add(text.index(start))
            if text.count(end) == 1:
                cuts.add(text.index(end) + len(end))
        boundaries = sorted(cuts)
        for left, right in zip(boundaries, boundaries[1:]):
            fragment = text[left:right].strip()
            # A cut immediately after "B: " must carry that label with the next
            # spoken words. Leaving a dangling label would make it part of A's text.
            fragment = re.sub(r"(?:^|\s)[AB]:\s*$", "", fragment).strip()
            if not fragment:
                continue
            before = list(re.finditer(r"(?<!\S)([AB]):\s*", text[:left]))
            if before and not re.match(r"[AB]:\s*", fragment):
                fragment = before[-1].group(1) + ": " + fragment
            result.append((role, fragment))
    if spoken_text(" ".join(t for _, t in result)) != spoken_text(" ".join(t for _, t in units)):
        raise RuntimeError("derivative_boundary_split_changed_approved_text")
    return result


def _audio_rows(root: Path) -> list[dict]:
    voice = _read(root / "voice-sections.json")
    texts = {(str(s["id"]), int(c["chunk"])): str(c.get("text") or "")
             for s in voice["sections"] for c in s["chunks"]}
    rows = []
    for unit in _read(root / "timeline-first.json")["audio_units"]:
        row = dict(unit)
        row["text"] = texts.get((str(unit["section_id"]), int(unit["chunk"])), "")
        rows.append(row)
    return rows


def freeze_primary(root: Path) -> dict:
    """Persist SUCCESS and hashes before attempting ANY optional derivative work."""
    root = Path(root)
    primary = root / "primary-success.json"
    if primary.is_file():
        return _read(primary)
    manifest = _read(root / "run-manifest.json")
    if manifest.get("status") != "pass":
        raise RuntimeError("derivative_requires_successful_parent")
    required = INPUT_NAMES[:5]
    if any(not (root / name).is_file() for name in required):
        raise RuntimeError("saved_parent_input_missing")
    report = {"schema_version": 1, "status": "SUCCESS", "format": manifest["format"],
              "files": {name: _hash(root / name) for name in INPUT_NAMES if (root / name).is_file()},
              "source": "final_master_qc_pass_before_optional_derivative"}
    atomic_write_json(primary, report)
    return report


def prepare_retry_source(root: Path) -> None:
    """Save text/timing/caption events with the accepted audio; no network calls."""
    from .podcast_captions import build_events
    root = Path(root)
    context_path = root / "derivative-context.json"
    if not context_path.is_file():
        rows = _audio_rows(root)
        events, timing = build_events(root, fmt="short", caption_roles=CAPTION_ROLES)
        doc = _read(root / "production.json") if (root / "production.json").is_file() else {}
        atomic_write_json(context_path, {"schema_version": 1, "rows": rows, "events": events,
                                         "caption_timing": timing, "candidate": doc.get("derivative"),
                                         "format": _read(root / "timeline-first.json")["format"]})
    primary = _read(root / "primary-success.json")
    primary["files"]["derivative-context.json"] = _hash(context_path)
    atomic_write_json(root / "primary-success.json", primary)
    archive = root / "derivative-source.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as out:
        for name in (*INPUT_NAMES[1:], "primary-success.json"):
            if (root / name).is_file():
                out.write(root / name, name)


def verify_primary(root: Path) -> dict:
    primary = _read(root / "primary-success.json")
    if primary.get("status") != "SUCCESS":
        raise RuntimeError("parent_success_not_frozen")
    for name, digest in primary["files"].items():
        if name not in INPUT_NAMES or not (root / name).is_file() or _hash(root / name) != digest:
            raise RuntimeError("saved_parent_input_changed_or_missing:" + str(name))
    return primary


def _selection(context: Mapping[str, Any], doc: Mapping[str, Any]) -> tuple[dict | None, str]:
    rows = context["rows"]
    requested = context.get("candidate")
    if requested:
        start, end = requested["start_text"], requested["end_text"]
        if not all(re.search(r"[.!؟!]$", s.strip()) for s in (start, end)):
            return None, "candidate_requires_complete_sentence_boundaries"
        approved = spoken_text(" ".join(section_text(s) for s in doc.get("script", {}).get("sections", [])))
        if approved and (approved.count(start) != 1 or approved.count(end) != 1):
            return None, "candidate_text_missing_or_ambiguous_in_approved_script"
        if approved:
            position = approved.index(start)
            if position and approved[:position].rstrip()[-1] not in ".!؟!":
                return None, "candidate_starts_inside_a_sentence"
        starts = [i for i, u in enumerate(rows) if u["text"] and spoken_text(u["text"]).startswith(start)]
        ends = [i for i, u in enumerate(rows) if u["text"] and spoken_text(u["text"]).endswith(end)]
        if len(starts) != 1 or len(ends) != 1 or starts[0] > ends[0]:
            return None, "candidate_not_uniquely_aligned_to_saved_audio_units"
        selected = rows[starts[0]:ends[0] + 1]
        if any(u["role"] not in CAPTION_ROLES for u in selected):
            return None, "candidate_crosses_identity_or_silence"
    else:
        valid = [u for u in rows if u["role"] in CAPTION_ROLES and u["text"]
                 and 7 <= float(u["end"]) - float(u["start"]) <= 60]
        # The old measured Podcast promo is preferred when it exists.
        valid.sort(key=lambda u: (u["role"] != "promo_short", abs(float(u["end"])-float(u["start"])-25)))
        if not valid:
            return None, "no_existing_complete_7_60_second_topic_unit"
        selected = [valid[0]]
    duration = float(selected[-1]["end"]) - float(selected[0]["start"])
    if not 7 <= duration <= 60:
        return None, "candidate_duration_outside_7_60_seconds"
    text = " ".join(u["text"] for u in selected)
    if context["format"] == "podcast":
        turns = [t for s in doc.get("script", {}).get("sections", []) for t in s.get("turns", [])]
        if turns:
            from .media import _gemini38_dialogue_turns
            selected_turns = _gemini38_dialogue_turns(text)
            first = selected_turns[0][1] if selected_turns else ""
            last = selected_turns[-1][1] if selected_turns else ""
            if not first or not any(spoken_text(t["text"]).startswith(first) for t in turns) or not any(spoken_text(t["text"]).endswith(last) for t in turns):
                return None, "podcast_candidate_must_keep_complete_turn_boundaries"
        speakers = re.findall(r"(?<!\S)([AB]):", text)
        if speakers and speakers[-1] == "A":
            return None, "podcast_candidate_ends_with_unanswered_question"
    return {"start": float(selected[0]["start"]), "end": float(selected[-1]["end"]),
            "text": text, "section_id": selected[0]["section_id"],
            "selection_basis": "approved_exact_text_measured_audio_units" if requested else "existing_measured_topic_unit"}, ""


def attempt(root: Path, *, renderer=None) -> dict:
    """Optional extraction; failure is a report, never a parent-production error."""
    root = Path(root)
    prefix = "derived-short"
    base = {"schema_version": 1, "primary_status": "SUCCESS", "provider_calls_added": 0, "tts_calls_added": 0}
    temporary = []
    try:
        prefix = _prefix(root)
        freeze_primary(root)
        prepare_retry_source(root)
        verify_primary(root)
        context = _read(root / "derivative-context.json")
        doc = _read(root / "production.json") if (root / "production.json").is_file() else {}
        window, reason = _selection(context, doc)
        if window is None:
            report = {**base, "status": "skipped", "reason": reason}
        else:
            from .media import render_derived_short, inspect_final, _run
            from .podcast_captions import build_ass, build_srt
            from .short_timed_text import _filter_escape_path
            raw, final = root / f".{prefix}-raw.mp4", root / f".{prefix}-captioned.mp4"
            temporary += [raw, final]
            (renderer or render_derived_short)(root / "final.mp4", raw,
                start_seconds=window["start"], end_seconds=window["end"], maximum_seconds=60)
            events = [dict(e, start=round(float(e["start"])-window["start"], 3),
                            end=round(float(e["end"])-window["start"], 3)) for e in context["events"]
                      if float(e["start"]) >= window["start"]-0.01 and float(e["end"]) <= window["end"]+0.01]
            if not events:
                raise RuntimeError("derived_caption_events_missing")
            ass = root / f"{prefix}.ass"
            ass.write_text(build_ass(events, fmt="short"), encoding="utf-8")
            (root / f"{prefix}.srt").write_text(build_srt(events), encoding="utf-8")
            _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(raw),
                  "-vf", "ass='" + _filter_escape_path(ass) + "'", "-map", "0:v:0", "-map", "0:a:0",
                  "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-c:a", "copy", str(final)], timeout=900)
            qc = inspect_final(final)
            if (qc["width"], qc["height"]) != (1080, 1920) or abs(qc["duration_seconds"]-(window["end"]-window["start"])) > 0.15:
                raise RuntimeError("derived_short_stream_or_duration_mismatch")
            final.replace(root / f"{prefix}.mp4")
            atomic_write_json(root / f"{prefix}-qc.json", {**qc, "status": "pass"})
            report = {**base, **window, "status": "pass", "file": f"{prefix}.mp4", "width": 1080, "height": 1920,
                      "duration_seconds": qc["duration_seconds"], "caption_timing": context["caption_timing"],
                      "clip_boundary_timing": "exact_saved_audio_units", "word_level_alignment_claimed": False,
                      "final_master_qc_status": "pass"}
    except Exception as exc:
        report = {**base, "status": "failed", "reason": f"{type(exc).__name__}:{str(exc)[:220]}"}
    finally:
        for path in temporary:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
    try:
        atomic_write_json(root / f"{prefix}.json", report)
        atomic_write_json(root / "derivative-status.json", report)
        if report["status"] != "pass":
            (root / f"{prefix}.mp4").unlink(missing_ok=True)
            (root / f"{prefix}-qc.json").unlink(missing_ok=True)
    except OSError:
        pass  # An optional status-write failure cannot invalidate the accepted parent.
    return report


def restore_retry_source(archive: Path, root: Path) -> None:
    allowed = set(INPUT_NAMES[1:]) | {"primary-success.json"}
    with zipfile.ZipFile(archive) as source:
        names = source.namelist()
        if len(names) != len(set(names)) or not set(names) <= allowed or sum(i.file_size for i in source.infolist()) > 512*1024*1024:
            raise RuntimeError("unsafe_derivative_retry_source")
        for name in names:
            destination = root / name
            if destination.exists():
                raise RuntimeError("retry_source_would_replace_existing_input")
            destination.write_bytes(source.read(name))
    verify_primary(root)


def warning_text(report: Mapping[str, Any]) -> str:
    reason = str(report.get("reason") or "no_valid_clip")
    meanings = {
        "candidate_requires_complete_sentence_boundaries": "حدود المقطع لا تنتهي بجمل كاملة",
        "candidate_not_uniquely_aligned_to_saved_audio_units": "تعذر ربط الحدود النصية بتوقيت صوت محفوظ واضح",
        "candidate_text_missing_or_ambiguous_in_approved_script": "الحدود النصية مفقودة أو مكررة في النص المعتمد",
        "candidate_starts_inside_a_sentence": "بداية المقطع تقع وسط جملة",
        "candidate_crosses_identity_or_silence": "المقطع يمر عبر الهوية أو وقفة بنيوية",
        "candidate_duration_outside_7_60_seconds": "مدة المقطع خارج نطاق 7–60 ثانية",
        "no_existing_complete_7_60_second_topic_unit": "لا يوجد مقطع متصل صالح بمدة 7–60 ثانية",
        "podcast_candidate_must_keep_complete_turn_boundaries": "المقطع يقطع دور أحد المتحدثين",
        "podcast_candidate_ends_with_unanswered_question": "المقطع ينتهي بسؤال دون جوابه",
    }
    detail = meanings.get(reason, "تعذر الاستخراج أو المونتاج: " + reason[:180])
    return "⚠️ الفيديو الأساسي نجح وهو جاهز. لم يُنتج الشورت المشتق. السبب: " + detail + ". يمكن إعادة المشتق وحده دون توليد صوت جديد."


def main() -> None:
    parser = argparse.ArgumentParser(description="Retry ONLY a derivative from frozen successful parent inputs; no TTS.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--restore-source", type=Path)
    args = parser.parse_args()
    if args.restore_source:
        restore_retry_source(args.restore_source, args.output)
    print(json.dumps(attempt(args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
