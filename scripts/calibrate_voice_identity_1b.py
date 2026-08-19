from __future__ import annotations

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path
from typing import Any

from scripts.voice_identity_observer import (
    _candidate_windows,
    _cosine,
    _load_model,
    _load_profile,
    _normalize_vector,
    _pitch_summary,
    _reference_tensor,
)

MODE = "observe_only"
PHASE = "1B_calibration"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _even_starts(total: int, size: int, count: int) -> list[int]:
    if total <= size:
        return [0]
    last = total - size
    count = max(2, int(count))
    starts = [int(round(last * index / (count - 1))) for index in range(count)]
    return list(dict.fromkeys(starts))


def _embed(window):
    import torch

    model = _load_model()
    with torch.inference_mode():
        return _normalize_vector(model.encode_batch(window.unsqueeze(0), normalize=True).squeeze())


def _score_window(window, *, primary_ref, questioner_ref) -> dict[str, float]:
    import torch

    vector = _embed(window)
    rms = float(torch.sqrt(torch.mean(window.float() ** 2)))
    return {
        "charon_similarity": _cosine(vector, primary_ref),
        "orus_similarity": _cosine(vector, questioner_ref),
        "rms": round(rms, 6),
    }


def _summary(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {"count": 0, "min": None, "p25": None, "median": None, "mean": None, "p75": None, "max": None}
    ordered = sorted(float(value) for value in values)

    def percentile(q: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        pos = (len(ordered) - 1) * q
        lo = int(math.floor(pos))
        hi = int(math.ceil(pos))
        if lo == hi:
            return ordered[lo]
        frac = pos - lo
        return ordered[lo] * (1.0 - frac) + ordered[hi] * frac

    return {
        "count": len(ordered),
        "min": round(ordered[0], 6),
        "p25": round(percentile(0.25), 6),
        "median": round(statistics.median(ordered), 6),
        "mean": round(statistics.mean(ordered), 6),
        "p75": round(percentile(0.75), 6),
        "max": round(ordered[-1], 6),
    }


def _score_file(item: dict[str, Any]) -> dict[str, Any]:
    import torch

    path = Path(item["path"])
    profile = _load_profile()
    model = _load_model()
    signal = model.load_audio(str(path)).detach().cpu().float().reshape(-1)
    sample_rate = int(profile["embedding"]["sample_rate_hz"])
    window_seconds = float(profile["embedding"]["window_seconds"])
    size = max(1, int(round(window_seconds * sample_rate)))
    if signal.numel() < size:
        raise RuntimeError(f"calibration_audio_too_short:{path}")

    primary_ref = _reference_tensor("primary")
    questioner_ref = _reference_tensor("questioner")

    raw_windows = []
    for index, start in enumerate(_even_starts(int(signal.numel()), size, int(item.get("window_count", 9)))):
        window = signal[start:start + size]
        scores = _score_window(window, primary_ref=primary_ref, questioner_ref=questioner_ref)
        raw_windows.append({
            "index": index,
            "start_seconds": round(start / sample_rate, 3),
            "duration_seconds": round(window.numel() / sample_rate, 3),
            **scores,
        })

    observer_windows = []
    for label, start, window in _candidate_windows(signal, sample_rate, window_seconds):
        scores = _score_window(window, primary_ref=primary_ref, questioner_ref=questioner_ref)
        observer_windows.append({
            "window": label,
            "start_seconds": round(start / sample_rate, 3),
            "duration_seconds": round(window.numel() / sample_rate, 3),
            **scores,
        })

    charon = [float(row["charon_similarity"]) for row in raw_windows]
    orus = [float(row["orus_similarity"]) for row in raw_windows]
    result = {
        "label": item["label"],
        "class": item["class"],
        "human_label": item.get("human_label"),
        "run_id": item.get("run_id"),
        "artifact_id": item.get("artifact_id"),
        "path": str(path),
        "sha256": _sha256(path),
        "duration_seconds": round(signal.numel() / sample_rate, 3),
        "raw_windows": raw_windows,
        "observer_windows": observer_windows,
        "charon_distribution": _summary(charon),
        "orus_distribution": _summary(orus),
        "pitch": _pitch_summary(signal, sample_rate),
    }
    return result


def _class_distribution(items: list[dict[str, Any]], class_name: str) -> dict[str, Any]:
    values = [
        float(window["charon_similarity"])
        for item in items
        if item["class"] == class_name
        for window in item["raw_windows"]
    ]
    return _summary(values)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    samples = manifest.get("samples", [])
    if not samples:
        raise SystemExit("calibration manifest has no samples")

    measured = [_score_file(item) for item in samples]
    profile = _load_profile()
    document = {
        "schema_version": 1,
        "phase": PHASE,
        "mode": MODE,
        "calibration_only": True,
        "enforcement": "disabled",
        "thresholds": None,
        "profile_version": profile["profile_version"],
        "backend": profile["embedding"],
        "notes": [
            "Raw calibration evidence only; no threshold is proposed or enforced.",
            "Positive production-run count is zero because no full Charon production exists yet.",
            "Known rejected runs are human-labelled negatives for primary-narrator identity calibration.",
        ],
        "samples": measured,
        "class_distributions": {
            "positive_confirmed": _class_distribution(measured, "positive_confirmed"),
            "negative_human_rejected": _class_distribution(measured, "negative_human_rejected"),
            "secondary_control": _class_distribution(measured, "secondary_control"),
        },
    }
    args.output.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")

    print("VOICE_CALIBRATION_1B_BEGIN")
    print(json.dumps(document, ensure_ascii=False, indent=2))
    print("VOICE_CALIBRATION_1B_END")


if __name__ == "__main__":
    main()
