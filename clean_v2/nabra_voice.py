from __future__ import annotations

import re
from pathlib import Path
from typing import Any


NABRA_REPO_ID = "oddadmix/Nabra-82M-v0.1"
NABRA_VOICE = "af_msa"
NABRA_SPEED = 0.87
NABRA_SAMPLE_RATE = 24000
NABRA_ONSET_FADE_MS = 25
# Kokoro raw-phoneme inference is bounded below its 510-character model limit.
NABRA_MAX_INFER_CHARS = 500
NABRA_FRAGMENT_TARGET_CHARS = 440
NABRA_REFERENCE_PROFILE = "nabra-82m-v0.1:af_msa:0.87:native-pauses-v1"

_PUNCTUATION_CHARS = set(",.;:!?،؛؟…—")
_TASHKEEL_RE = re.compile("[ً-ْٰ]")


def _diacritize_preserving_explicit_marks(frontend: Any, text: str) -> str:
    """Use Camel MSA context while preserving writer-authored sparse tashkeel.

    The official Nabra frontend diacritizes unvowelled MSA before espeak. When
    the writer deliberately marks one ambiguous word, keep that exact token and
    use Camel's contextual result for the rest. If token alignment changes, keep
    the writer's original marked text rather than risk overwriting pronunciation.
    """
    source = " ".join(str(text or "").split()).strip()
    if not source:
        return ""
    candidate = " ".join(str(frontend.diacritize(source) or "").split()).strip()
    if not candidate:
        return source

    source_tokens = source.split()
    candidate_tokens = candidate.split()
    has_explicit = any(_TASHKEEL_RE.search(token) for token in source_tokens)
    if not has_explicit:
        return candidate
    if len(source_tokens) != len(candidate_tokens):
        return source

    merged = [
        original if _TASHKEEL_RE.search(original) else generated
        for original, generated in zip(source_tokens, candidate_tokens)
    ]
    return " ".join(merged)


def _lexical_only(value: str) -> str:
    return " ".join(
        "".join(ch for ch in value if ch not in _PUNCTUATION_CHARS).split()
    )


def _native_pause_marker(role: str, *, final: bool) -> str:
    """Return only model-native Kokoro punctuation; never external silence.

    The listener-approved 0.87 probe used a stronger structural beat at major
    idea boundaries while keeping prayer/identity transitions short and fluid.
    """
    if final:
        return "…"
    if role == "hook":
        return "… —"
    if role in {"prayer", "channel_identity"}:
        return ","
    return "…"


def _strip_terminal_model_punctuation(phonemes: str) -> str:
    return re.sub(r"[\s\.,;:!?،؛؟…—]+$", "", str(phonemes or "")).strip()


def _g2p_preserving_breath_punctuation(pipeline: Any, text: str) -> str:
    """Preserve writer-authored light-breath punctuation through Arabic G2P.

    The pinned Arabic frontend already handles lexical pronunciation and normal
    sentence-final punctuation well, but it can drop commas/semicolons/colons.
    Split only at those light-breath marks, pass every lexical span to the same
    G2P unchanged (including intentional minimal tashkeel), then restore a single
    Kokoro comma token. Acoustic synthesis still happens once for the full pass.
    """
    source = " ".join(str(text or "").split()).strip()
    if not source:
        return ""

    parts = re.split(r"([،,؛;:])", source)
    out: list[str] = []
    for part in parts:
        if not part:
            continue
        if re.fullmatch(r"[،,؛;:]", part):
            # One model-native light-breath token; never stack punctuation.
            out.append(",")
            continue
        lexical = part.strip()
        if not lexical:
            continue
        phonemes, _extra = pipeline.g2p(lexical)
        phonemes = str(phonemes or "").strip()
        if not phonemes:
            raise RuntimeError("nabra_empty_lexical_phonemes")
        out.append(phonemes)

    return " ".join(out).strip()


class NabraVoiceSynthesizer:
    """Listener-approved local Nabra-82M adapter.

    Contract:
    - af_msa at native model speed 0.87;
    - one inference when the narration fits Kokoro; otherwise the fewest bounded passes;
    - model-native punctuation pauses only (no inserted waveform silence);
    - one global 25 ms onset fade, never one fade per sentence/chunk;
    - no EQ/compressor/tempo/pitch processing here.
    """

    def __init__(self) -> None:
        self._runtime: tuple[object, object, object, object] | None = None
        self._msa_diacritizer_available: bool | None = None

    def _load(self) -> tuple[object, object, object, object]:
        if self._runtime is not None:
            return self._runtime

        try:
            import torch
            from huggingface_hub import hf_hub_download
            from kokoro import KModel, KPipeline
            from kokoro import pipeline as kpipeline_mod
            from scripts.arabic_g2p import (
                ArabicG2P,
                EXTRA_SYMBOLS,
                clean_phonemes,
                normalize_text,
            )
        except ImportError as exc:
            raise RuntimeError(f"nabra_runtime_missing:{type(exc).__name__}") from exc

        model_path = hf_hub_download(NABRA_REPO_ID, "kokoro_arabic.pth")
        config_path = hf_hub_download(NABRA_REPO_ID, "config.json")
        voice_path = hf_hub_download(NABRA_REPO_ID, "af_msa.pt")

        model = KModel(
            repo_id=NABRA_REPO_ID,
            config=config_path,
            model=model_path,
            disable_complex=True,
        ).eval()
        model.vocab.update(EXTRA_SYMBOLS)
        kpipeline_mod.LANG_CODES.setdefault("ar", "ar")
        pipeline = KPipeline(lang_code="ar", repo_id=NABRA_REPO_ID, model=model)

        original_g2p = pipeline.g2p
        arabic_frontend = ArabicG2P(diacritize=True)
        self._msa_diacritizer_available = bool(
            getattr(arabic_frontend, "diac_available", False)
        )
        if not self._msa_diacritizer_available:
            raise RuntimeError("nabra_msa_diacritizer_unavailable")

        def verified_g2p(text: str):
            normalized, _latin_dropped = normalize_text(text)
            prepared = _diacritize_preserving_explicit_marks(
                arabic_frontend,
                normalized,
            )
            phonemes, extra = original_g2p(prepared)
            return clean_phonemes(phonemes), extra

        pipeline.g2p = verified_g2p
        voice = torch.load(voice_path, map_location="cpu", weights_only=True)
        self._runtime = (pipeline, voice, torch, model)
        return self._runtime

    @staticmethod
    def _normalize_parts(parts: list[dict[str, Any]]) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        for index, raw in enumerate(parts, start=1):
            role = str(raw.get("role") or "topic").strip() or "topic"
            text = " ".join(str(raw.get("text") or "").split()).strip()
            if not text:
                raise RuntimeError(f"nabra_empty_part:{index}")
            if re.search(r"(?m)^\s*[AB]:\s*\S", text):
                raise RuntimeError("nabra_dialogue_not_supported")
            normalized.append({"role": role, "text": text})
        if not normalized:
            raise RuntimeError("nabra_empty_transcript")
        return normalized

    @staticmethod
    def _split_phonemes_for_model_limit(
        phonemes: str,
        *,
        target_chars: int = NABRA_FRAGMENT_TARGET_CHARS,
    ) -> list[tuple[str, str | None]]:
        """Split only when Kokoro's raw-phoneme limit requires it.

        Each returned tuple is (spoken_phonemes, technical_boundary_marker).
        The final tuple has marker=None so the caller can apply the semantic
        role marker. Prefer existing punctuation; otherwise split at whitespace
        and use one light native comma. No waveform silence is ever inserted.
        """
        remaining = str(phonemes or "").strip()
        if not remaining:
            return []

        result: list[tuple[str, str | None]] = []
        minimum_useful_cut = max(80, int(target_chars * 0.55))
        punctuation = (("…", "…"), (".", "."), ("?", "?"), ("!", "!"), (",", ","))

        while len(remaining) > target_chars:
            window = remaining[: target_chars + 1]
            best_pos = -1
            best_marker: str | None = None
            for symbol, marker in punctuation:
                pos = window.rfind(symbol)
                if pos >= minimum_useful_cut and pos > best_pos:
                    best_pos = pos
                    best_marker = marker

            if best_pos >= minimum_useful_cut:
                spoken = _strip_terminal_model_punctuation(
                    remaining[:best_pos]
                )
                rest = remaining[best_pos + 1 :].strip()
                marker = best_marker or ","
            else:
                cut = window.rfind(" ")
                if cut < minimum_useful_cut:
                    raise RuntimeError(
                        "nabra_phoneme_fragment_has_no_safe_boundary:"
                        + str(len(remaining))
                    )
                spoken = remaining[:cut].strip()
                rest = remaining[cut + 1 :].strip()
                marker = ","

            if not spoken or not rest:
                raise RuntimeError("nabra_phoneme_fragment_split_invalid")
            result.append((spoken, marker))
            remaining = rest

        if remaining:
            result.append((remaining, None))
        return result

    def synthesize_continuous(
        self,
        parts: list[dict[str, Any]],
        output_path: Path,
    ) -> dict[str, Any]:
        """Synthesize one continuous narration stream with bounded Nabra inference.

        Short narration normally fits in one Kokoro inference. Long film/podcast
        narration is packed into the fewest possible <=500-character phoneme
        batches, split only at safe punctuation/whitespace boundaries. The final
        WAV is one uninterrupted PCM stream: no external silence, no per-sentence
        fade, no tempo/pitch processing, and only one global onset fade.
        """
        normalized = self._normalize_parts(parts)
        pipeline, voice, torch, model = self._load()

        try:
            import numpy as np
            import soundfile as sf
        except ImportError as exc:
            raise RuntimeError(
                "nabra_audio_runtime_missing:" + type(exc).__name__
            ) from exc

        def recognized_count(value: str) -> int:
            return sum(1 for ch in value if model.vocab.get(ch) is not None)

        fragments: list[dict[str, Any]] = []
        for part_index, item in enumerate(normalized):
            raw_phonemes = _g2p_preserving_breath_punctuation(
                pipeline,
                item["text"],
            )
            if not raw_phonemes:
                raise RuntimeError(
                    "nabra_empty_phonemes:" + str(part_index + 1)
                )

            spoken_phonemes = _strip_terminal_model_punctuation(raw_phonemes)
            if not spoken_phonemes:
                raise RuntimeError(
                    "nabra_empty_lexical_phonemes:" + str(part_index + 1)
                )
            if _lexical_only(spoken_phonemes) != _lexical_only(raw_phonemes):
                raise RuntimeError(
                    "nabra_native_pause_tokens_changed_lexical_phonemes"
                )

            split_fragments = self._split_phonemes_for_model_limit(
                spoken_phonemes
            )
            if not split_fragments:
                raise RuntimeError(
                    "nabra_empty_split_phonemes:" + str(part_index + 1)
                )

            for fragment_index, (fragment_phonemes, technical_marker) in enumerate(
                split_fragments
            ):
                is_part_final = fragment_index == len(split_fragments) - 1
                marker = (
                    _native_pause_marker(
                        item["role"],
                        final=part_index == len(normalized) - 1,
                    )
                    if is_part_final
                    else (technical_marker or ",")
                )
                rendered = (fragment_phonemes + " " + marker).strip()
                if len(rendered) > NABRA_MAX_INFER_CHARS:
                    raise RuntimeError(
                        "nabra_phoneme_fragment_exceeds_model_limit:"
                        + str(len(rendered))
                    )
                fragments.append(
                    {
                        "part_index": part_index,
                        "role": item["role"],
                        "text": item["text"],
                        "phonemes": fragment_phonemes,
                        "marker": marker,
                        "is_part_final": is_part_final,
                    }
                )

        batches: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        current_text = ""
        for fragment in fragments:
            rendered = (
                str(fragment["phonemes"])
                + " "
                + str(fragment["marker"])
            ).strip()
            candidate = (
                (current_text + " " + rendered).strip()
                if current_text
                else rendered
            )
            if current and len(candidate) > NABRA_MAX_INFER_CHARS:
                batches.append(current)
                current = []
                current_text = ""
                candidate = rendered
            if len(candidate) > NABRA_MAX_INFER_CHARS:
                raise RuntimeError(
                    "nabra_inference_batch_exceeds_model_limit:"
                    + str(len(candidate))
                )
            current.append(fragment)
            current_text = candidate
        if current:
            batches.append(current)
        if not batches:
            raise RuntimeError("nabra_empty_inference_batches")

        voice_pack = voice.to(model.device)
        audio_batches: list[Any] = []
        fragment_marks: list[dict[str, Any]] = []
        global_sample_offset = 0
        phoneme_batches: list[str] = []

        with torch.inference_mode():
            for batch_index, batch in enumerate(batches, start=1):
                pieces: list[str] = []
                local_marks: list[dict[str, Any]] = []
                for fragment in batch:
                    before = " ".join(pieces).strip()
                    start_token = recognized_count(before)

                    pieces.append(str(fragment["phonemes"]))
                    through_speech = " ".join(pieces).strip()
                    speech_end_token = recognized_count(through_speech)

                    pieces.append(str(fragment["marker"]))
                    through_pause = " ".join(pieces).strip()
                    pause_end_token = recognized_count(through_pause)
                    if pause_end_token <= speech_end_token:
                        raise RuntimeError(
                            "nabra_native_pause_token_not_admitted:role="
                            + str(fragment["role"])
                        )
                    local_marks.append(
                        {
                            **fragment,
                            "start_token": start_token,
                            "speech_end_token": speech_end_token,
                            "pause_end_token": pause_end_token,
                        }
                    )

                batch_phonemes = " ".join(pieces).strip()
                if len(batch_phonemes) > NABRA_MAX_INFER_CHARS:
                    raise RuntimeError(
                        "nabra_inference_batch_exceeds_model_limit:"
                        + str(len(batch_phonemes))
                    )
                phoneme_batches.append(batch_phonemes)

                result = type(pipeline).infer(
                    model,
                    batch_phonemes,
                    voice_pack,
                    speed=NABRA_SPEED,
                )
                if result.pred_dur is None:
                    raise RuntimeError(
                        "nabra_pred_dur_missing:batch=" + str(batch_index)
                    )
                batch_audio = (
                    result.audio.detach().cpu().numpy().astype("float32")
                )
                if batch_audio.size < 256:
                    raise RuntimeError(
                        "nabra_audio_too_short:batch=" + str(batch_index)
                    )

                durations = result.pred_dur.detach().cpu().long()
                chars = [
                    ch
                    for ch in batch_phonemes
                    if model.vocab.get(ch) is not None
                ]
                if durations.numel() != len(chars) + 2:
                    raise RuntimeError(
                        "nabra_duration_token_mismatch:batch="
                        + str(batch_index)
                        + ":durations="
                        + str(durations.numel())
                        + ":chars="
                        + str(len(chars))
                    )
                token_durations = durations[1:-1]
                cumulative_frames = [0]
                total_frames = 0
                for value in token_durations.tolist():
                    total_frames += int(value)
                    cumulative_frames.append(total_frames)

                def sample_at(token_count: int) -> int:
                    if (
                        token_count < 0
                        or token_count >= len(cumulative_frames)
                    ):
                        raise RuntimeError(
                            "nabra_invalid_token_mark:"
                            + str(token_count)
                        )
                    return min(
                        int(batch_audio.size),
                        cumulative_frames[token_count] * 600,
                    )

                for local_index, item in enumerate(local_marks):
                    start_sample = sample_at(int(item["start_token"]))
                    speech_end_sample = sample_at(
                        int(item["speech_end_token"])
                    )
                    pause_end_sample = sample_at(
                        int(item["pause_end_token"])
                    )
                    if local_index == 0:
                        start_sample = 0
                    if local_index == len(local_marks) - 1:
                        # Keep Kokoro's own batch tail attached to the last
                        # native pause. It is model output, not inserted silence.
                        pause_end_sample = int(batch_audio.size)

                    start_sample = max(0, start_sample)
                    speech_end_sample = max(
                        start_sample + 1,
                        speech_end_sample,
                    )
                    pause_end_sample = min(
                        int(batch_audio.size),
                        max(speech_end_sample + 1, pause_end_sample),
                    )
                    if pause_end_sample <= speech_end_sample:
                        raise RuntimeError(
                            "nabra_native_pause_has_no_duration:role="
                            + str(item["role"])
                        )

                    fragment_marks.append(
                        {
                            **item,
                            "batch": batch_index,
                            "start_sample": (
                                global_sample_offset + start_sample
                            ),
                            "speech_end_sample": (
                                global_sample_offset + speech_end_sample
                            ),
                            "pause_end_sample": (
                                global_sample_offset + pause_end_sample
                            ),
                        }
                    )

                audio_batches.append(batch_audio)
                global_sample_offset += int(batch_audio.size)

        audio = (
            audio_batches[0].copy()
            if len(audio_batches) == 1
            else np.concatenate(audio_batches).astype("float32", copy=False)
        )
        if audio.size < 256:
            raise RuntimeError("nabra_audio_too_short")

        # Listener-approved onset softening remains global only. Never fade each
        # bounded inference batch because that would recreate audible restarts.
        fade_samples = min(
            int(audio.size),
            int(
                round(
                    NABRA_SAMPLE_RATE
                    * NABRA_ONSET_FADE_MS
                    / 1000.0
                )
            ),
        )
        if fade_samples > 1:
            audio = audio.copy()
            audio[:fade_samples] *= np.linspace(
                0.0,
                1.0,
                fade_samples,
                dtype=np.float32,
            )

        marks: list[dict[str, Any]] = []
        for part_index, item in enumerate(normalized):
            part_fragments = [
                mark
                for mark in fragment_marks
                if int(mark["part_index"]) == part_index
            ]
            if not part_fragments:
                raise RuntimeError(
                    "nabra_part_timing_missing:" + str(part_index + 1)
                )
            first = part_fragments[0]
            last = part_fragments[-1]
            start_sample = int(first["start_sample"])
            speech_end_sample = int(last["speech_end_sample"])
            pause_end_sample = int(last["pause_end_sample"])
            if pause_end_sample <= speech_end_sample:
                raise RuntimeError(
                    "nabra_native_pause_has_no_duration:role="
                    + str(item["role"])
                )
            marks.append(
                {
                    "role": item["role"],
                    "text": item["text"],
                    "native_pause_marker": str(last["marker"]),
                    "fragment_count": len(part_fragments),
                    "start_sample": start_sample,
                    "speech_end_sample": speech_end_sample,
                    "pause_end_sample": pause_end_sample,
                    "start_seconds": round(
                        start_sample / NABRA_SAMPLE_RATE,
                        6,
                    ),
                    "speech_end_seconds": round(
                        speech_end_sample / NABRA_SAMPLE_RATE,
                        6,
                    ),
                    "pause_end_seconds": round(
                        pause_end_sample / NABRA_SAMPLE_RATE,
                        6,
                    ),
                    "native_pause_seconds": round(
                        (
                            pause_end_sample
                            - speech_end_sample
                        )
                        / NABRA_SAMPLE_RATE,
                        6,
                    ),
                }
            )

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_name(
            "." + output_path.name + ".nabra.tmp.wav"
        )
        try:
            sf.write(
                temporary,
                audio,
                NABRA_SAMPLE_RATE,
                subtype="PCM_16",
                format="WAV",
            )
            if (
                not temporary.is_file()
                or temporary.stat().st_size < 1024
            ):
                raise RuntimeError("nabra_output_missing_or_empty")
            temporary.replace(output_path)
        finally:
            temporary.unlink(missing_ok=True)

        return {
            "path": output_path,
            "profile": NABRA_REFERENCE_PROFILE,
            "voice": NABRA_VOICE,
            "speed": NABRA_SPEED,
            "sample_rate": NABRA_SAMPLE_RATE,
            "single_continuous_inference": len(batches) == 1,
            "continuous_narration_stream": True,
            "inference_passes": len(batches),
            "bounded_inference": len(batches) > 1,
            "max_infer_chars": NABRA_MAX_INFER_CHARS,
            "external_silence_insertions": 0,
            "tempo_or_pitch_change": False,
            "native_pause_tokens": True,
            "msa_diacritizer": "camel-tools:calima-msa-r13",
            "msa_diacritizer_available": bool(self._msa_diacritizer_available),
            "sparse_writer_tashkeel_preserved": True,
            "phoneme_batches": phoneme_batches,
            "parts": marks,
        }

    def synthesize(self, transcript: str, output_path: Path) -> Path:
        text = " ".join(str(transcript or "").split()).strip()
        result = self.synthesize_continuous(
            [{"role": "narration", "text": text}],
            output_path,
        )
        return Path(result["path"])
