from __future__ import annotations

import json
import math
import os
import statistics
from pathlib import Path
from typing import Any

import isco_video_agent.orchestrator as orchestrator

from scripts import voice_mesh

MODE = "observe_only"
PROFILE_PATH = Path(__file__).resolve().parents[1] / "voice-profiles" / "voice-reference-profile-v1.json"
AUDIT_FILENAME = "voice-identity-audit.json"

_model = None
_profile: dict[str, Any] | None = None
_original_synthesize_tts_section = None


def _load_profile() -> dict[str, Any]:
    global _profile
    if _profile is None:
        data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
        if data.get("profile_version") != "channel-voice-roster-v1":
            raise RuntimeError("voice_reference_profile_version_mismatch")
        if set(data.get("profiles", {})) != {"primary", "questioner"}:
            raise RuntimeError("voice_reference_profiles_invalid")
        _profile = data
    return _profile


def _load_model():
    global _model
    if _model is None:
        from speechbrain.inference.classifiers import EncoderClassifier
        from speechbrain.utils.fetching import FetchConfig

        profile = _load_profile()
        embedding = profile["embedding"]
        cache_root = Path(os.environ.get("RUNNER_TEMP", ".cache")) / "voice-observer-ecapa"
        _model = EncoderClassifier.from_hparams(
            source=str(embedding["model_source"]),
            savedir=str(cache_root),
            fetch_config=FetchConfig(
                revision=str(embedding["model_revision"]),
                allow_updates=False,
            ),
            run_opts={"device": "cpu"},
        )
    return _model


def _normalize_vector(vector):
    import torch

    vector = vector.detach().cpu().float().reshape(-1)
    norm = torch.linalg.vector_norm(vector)
    if not torch.isfinite(norm) or float(norm) <= 0:
        raise RuntimeError("voice_embedding_invalid_norm")
    return vector / norm


def _reference_tensor(role: str):
    import torch

    profile = _load_profile()
    values = profile["profiles"][role]["centroid"]
    return _normalize_vector(torch.tensor(values, dtype=torch.float32))


def _cosine(left, right) -> float:
    import torch

    return round(float(torch.dot(_normalize_vector(left), _normalize_vector(right))), 6)


def _candidate_windows(signal, sample_rate: int, window_seconds: float):
    size = max(1, int(round(window_seconds * sample_rate)))
    total = int(signal.numel())
    if total <= size:
        return [("full", 0, signal)]
    starts = [
        ("start", 0),
        ("middle", max(0, (total - size) // 2)),
        ("end", max(0, total - size)),
    ]
    result = []
    seen: set[int] = set()
    for label, start in starts:
        if start in seen:
            continue
        seen.add(start)
        result.append((label, start, signal[start:start + size]))
    return result


def _pitch_summary(signal, sample_rate: int) -> dict[str, float | None]:
    """Secondary diagnostic only; speaker identity is always ECAPA similarity."""
    try:
        import torch
        import torchaudio.functional as AF

        if signal.numel() < sample_rate:
            return {"f0_median": None, "f0_p10": None, "f0_p90": None}
        pitch = AF.detect_pitch_frequency(
            signal.unsqueeze(0),
            sample_rate=sample_rate,
            frame_time=0.02,
            win_length=15,
            freq_low=70,
            freq_high=350,
        ).reshape(-1)
        pitch = pitch[torch.isfinite(pitch) & (pitch > 0)]
        if pitch.numel() < 5:
            return {"f0_median": None, "f0_p10": None, "f0_p90": None}
        pitch = torch.sort(pitch).values

        def quantile(q: float) -> float:
            idx = min(int(round((pitch.numel() - 1) * q)), pitch.numel() - 1)
            return round(float(pitch[idx]), 2)

        return {
            "f0_median": quantile(0.50),
            "f0_p10": quantile(0.10),
            "f0_p90": quantile(0.90),
        }
    except Exception:
        return {"f0_median": None, "f0_p10": None, "f0_p90": None}


def _dialogue_mode(transcript: str) -> bool:
    if os.environ.get("ISCO_DIALOGUE_QA") == "1":
        return True
    speakers = set()
    for line in transcript.splitlines():
        stripped = line.strip()
        if stripped.startswith("A:"):
            speakers.add("A")
        elif stripped.startswith("B:"):
            speakers.add("B")
        elif stripped.startswith("السائل:"):
            speakers.add("questioner")
        elif stripped.startswith("المجيب:"):
            speakers.add("responder")
    return len(speakers) >= 2


def _actual_provider(output: Path) -> dict[str, Any]:
    try:
        return voice_mesh.consume_voice_provenance(output)
    except Exception:
        return {"provider": "unknown", "fallback_used": None}


def _analyze_wav(output: Path, *, dialogue: bool) -> dict[str, Any]:
    import torch

    profile = _load_profile()
    model = _load_model()
    signal = model.load_audio(str(output)).detach().cpu().float().reshape(-1)
    sample_rate = int(profile["embedding"]["sample_rate_hz"])
    if signal.numel() < sample_rate:
        raise RuntimeError("voice_audit_audio_too_short")

    primary_ref = _reference_tensor("primary")
    questioner_ref = _reference_tensor("questioner")
    window_seconds = float(profile["embedding"]["window_seconds"])
    scores = []
    vectors = []
    for label, start, window in _candidate_windows(signal, sample_rate, window_seconds):
        with torch.inference_mode():
            vector = _normalize_vector(model.encode_batch(window.unsqueeze(0), normalize=True).squeeze())
        vectors.append(vector)
        item = {
            "window": label,
            "start_seconds": round(start / sample_rate, 3),
            "duration_seconds": round(window.numel() / sample_rate, 3),
            "primary_similarity": _cosine(vector, primary_ref),
        }
        if dialogue:
            item["questioner_similarity"] = _cosine(vector, questioner_ref)
        scores.append(item)

    pairwise = []
    for i in range(len(vectors)):
        for j in range(i + 1, len(vectors)):
            pairwise.append(_cosine(vectors[i], vectors[j]))

    primary_scores = [float(item["primary_similarity"]) for item in scores]
    result: dict[str, Any] = {
        "speaker_similarity": round(statistics.median(primary_scores), 6) if not dialogue else None,
        "window_scores": scores,
        "internal_consistency": {
            "median": round(statistics.median(pairwise), 6) if pairwise else 1.0,
            "minimum": round(min(pairwise), 6) if pairwise else 1.0,
        },
        "dialogue_role_segmentation": "not_time_aligned_v1" if dialogue else None,
    }
    result.update(_pitch_summary(signal, sample_rate))
    return result


def _audit_path(output: Path) -> Path:
    # Engine writes section WAVs to <production-root>/audio/NN.wav.
    return output.parent.parent / AUDIT_FILENAME


def _base_document() -> dict[str, Any]:
    profile = _load_profile()
    return {
        "schema_version": 1,
        "mode": MODE,
        "calibrated": False,
        "enforcement": "disabled",
        "profile_version": profile["profile_version"],
        "backend": profile["embedding"],
        "roster": {
            "primary": profile["profiles"]["primary"]["voice_name"],
            "questioner": profile["profiles"]["questioner"]["voice_name"],
            "inner_dialogue": profile["profiles"]["primary"]["voice_name"],
        },
        "sections": [],
        "summary": {"sections_seen": 0, "audit_errors": 0},
    }


def _append_entry(path: Path, entry: dict[str, Any]) -> None:
    try:
        document = json.loads(path.read_text(encoding="utf-8")) if path.exists() else _base_document()
        if not isinstance(document, dict) or not isinstance(document.get("sections"), list):
            document = _base_document()
        document["sections"].append(entry)
        document["summary"] = {
            "sections_seen": len(document["sections"]),
            "audit_errors": sum(1 for item in document["sections"] if item.get("decision") == "audit_error"),
        }
        path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"Voice Identity Observer diagnostics write skipped ({type(exc).__name__})")


def observe_output(
    *,
    task_id: str,
    transcript: str,
    output: Path,
    model: str,
    requested_voice: str,
) -> None:
    dialogue = _dialogue_mode(transcript)
    provenance = _actual_provider(output)
    expected_roles = ["primary", "questioner"] if dialogue else ["primary"]
    entry: dict[str, Any] = {
        "task_id": task_id,
        "section": task_id.removeprefix("TTS_SECTION_").lower(),
        "provider": provenance.get("provider", "unknown"),
        "model": model,
        "requested_voice": requested_voice,
        "dialogue_mode": dialogue,
        "fallback_used": provenance.get("fallback_used"),
        "reference_profiles": expected_roles,
        "mode": MODE,
        "decision": "uncalibrated",
    }
    try:
        entry.update(_analyze_wav(output, dialogue=dialogue))
    except Exception as exc:
        entry.update({
            "speaker_similarity": None,
            "window_scores": [],
            "internal_consistency": None,
            "f0_median": None,
            "f0_p10": None,
            "f0_p90": None,
            "decision": "audit_error",
            "audit_error": f"{type(exc).__name__}: {str(exc)[:240]}",
        })
    _append_entry(_audit_path(output), entry)
    if entry["decision"] == "audit_error":
        print(f"Voice Identity Observer: {task_id} audit_error (production unchanged)")
    else:
        print(f"Voice Identity Observer: {task_id} measured; decision=uncalibrated observe_only")


def install_voice_identity_observer() -> None:
    """Wrap the common post-TTS boundary. Never retries, blocks, or changes provider choice."""
    global _original_synthesize_tts_section
    current = orchestrator._synthesize_tts_section
    # Only our literal marker means the wrapper is already installed. This avoids
    # truthy proxy/mock attributes being mistaken for installation state.
    if getattr(current, "_is_voice_identity_observer", False) is True:
        return
    _original_synthesize_tts_section = current

    def wrapped(
        ledger,
        circuit,
        budget,
        *,
        task_id: str,
        api_key: str,
        transcript: str,
        output: Path,
        model: str,
        voice: str,
        style: str,
    ) -> Path:
        # The production TTS call happens exactly once through the original boundary.
        result = _original_synthesize_tts_section(
            ledger,
            circuit,
            budget,
            task_id=task_id,
            api_key=api_key,
            transcript=transcript,
            output=output,
            model=model,
            voice=voice,
            style=style,
        )
        try:
            observe_output(
                task_id=task_id,
                transcript=transcript,
                output=Path(result),
                model=model,
                requested_voice=voice,
            )
        except Exception as exc:
            # Phase 1 is strictly fail-open: observer failure cannot alter production.
            print(f"Voice Identity Observer skipped ({type(exc).__name__}); production unchanged")
        return result

    wrapped._is_voice_identity_observer = True
    orchestrator._synthesize_tts_section = wrapped
    print("Voice Identity Observer V1 installed: observe_only, uncalibrated, no retry/block/provider changes")
