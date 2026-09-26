from __future__ import annotations

import re
from pathlib import Path
from typing import Any


NABRA_REPO_ID = "oddadmix/Nabra-82M-v0.1"
NABRA_VOICE = "af_msa"
NABRA_SPEED = 0.87
NABRA_SAMPLE_RATE = 24000
NABRA_ONSET_FADE_MS = 25
NABRA_REFERENCE_PROFILE = "nabra-82m-v0.1:af_msa:0.87:native-pauses-v1"

_PUNCTUATION_CHARS = set(",.;:!?،؛؟…—")


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
        if not part.strip():
            continue
        phonemes, _extra = pipeline.g2p(part)
        phonemes = str(phonemes or "").strip()
        if not phonemes:
            raise RuntimeError("nabra_empty_lexical_phonemes")
        out.append(phonemes)

    return " ".join(out).strip()


class NabraVoiceSynthesizer:
    """Listener-approved local Nabra-82M adapter.

    Contract:
    - af_msa at native model speed 0.87;
    - one continuous inference for a complete Nabra narration pass;
    - model-native punctuation pauses only (no inserted waveform silence);
    - one global 25 ms onset fade, never one fade per sentence/chunk;
    - no EQ/compressor/tempo/pitch processing here.
    """

    def __init__(self) -> None:
        self._runtime: tuple[object, object, object, object] | None = None

    def _load(self) -> tuple[object, object, object, object]:
        if self._runtime is not None:
            return self._runtime

        try:
            import torch
            from huggingface_hub import hf_hub_download
            from kokoro import KModel, KPipeline
            from kokoro import pipeline as kpipeline_mod
            from scripts.arabic_g2p import EXTRA_SYMBOLS, clean_phonemes
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

        def verified_g2p(text: str):
            phonemes, extra = original_g2p(text)
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

    def synthesize_continuous(
        self,
        parts: list[dict[str, Any]],
        output_path: Path,
    ) -> dict[str, Any]:
        """Synthesize all parts in one Nabra inference and return measured marks.

        The returned part marks are derived from Kokoro's own pred_dur timeline.
        They are timing evidence only; the final narration waveform is never cut
        and re-concatenated.
        """
        normalized = self._normalize_parts(parts)
        pipeline, voice, torch, model = self._load()

        pieces: list[str] = []
        token_marks: list[dict[str, Any]] = []

        def recognized_count(value: str) -> int:
            return sum(1 for ch in value if model.vocab.get(ch) is not None)

        for index, item in enumerate(normalized):
            raw_phonemes = _g2p_preserving_breath_punctuation(
                pipeline,
                item["text"],
            )
            if not raw_phonemes:
                raise RuntimeError(f"nabra_empty_phonemes:{index + 1}")

            spoken_phonemes = _strip_terminal_model_punctuation(raw_phonemes)
            if not spoken_phonemes:
                raise RuntimeError(f"nabra_empty_lexical_phonemes:{index + 1}")
            if _lexical_only(spoken_phonemes) != _lexical_only(raw_phonemes):
                raise RuntimeError("nabra_native_pause_tokens_changed_lexical_phonemes")

            before = " ".join(pieces).strip()
            start_token = recognized_count(before)
            pieces.append(spoken_phonemes)
            through_speech = " ".join(pieces).strip()
            speech_end_token = recognized_count(through_speech)

            marker = _native_pause_marker(
                item["role"],
                final=index == len(normalized) - 1,
            )
            pieces.append(marker)
            through_pause = " ".join(pieces).strip()
            pause_end_token = recognized_count(through_pause)
            if pause_end_token <= speech_end_token:
                raise RuntimeError(
                    f"nabra_native_pause_token_not_admitted:role={item['role']}"
                )

            token_marks.append(
                {
                    "role": item["role"],
                    "text": item["text"],
                    "start_token": start_token,
                    "speech_end_token": speech_end_token,
                    "pause_end_token": pause_end_token,
                    "native_pause_marker": marker,
                }
            )

        phonemes = " ".join(pieces).strip()
        with torch.inference_mode():
            result = type(pipeline).infer(
                model,
                phonemes,
                voice.to(model.device),
                speed=NABRA_SPEED,
            )

        if result.pred_dur is None:
            raise RuntimeError("nabra_pred_dur_missing")
        audio = result.audio.detach().cpu().numpy().astype("float32")
        if audio.size < 256:
            raise RuntimeError("nabra_audio_too_short")

        # The approved probe used one global onset softening only. This preserves
        # every speech sample after the very beginning and avoids sentence-onset
        # artifacts from chunk-by-chunk synthesis.
        fade_samples = min(
            int(audio.size),
            int(round(NABRA_SAMPLE_RATE * NABRA_ONSET_FADE_MS / 1000.0)),
        )
        if fade_samples > 1:
            try:
                import numpy as np
            except ImportError as exc:
                raise RuntimeError(
                    f"nabra_audio_runtime_missing:{type(exc).__name__}"
                ) from exc
            audio = audio.copy()
            audio[:fade_samples] *= np.linspace(
                0.0,
                1.0,
                fade_samples,
                dtype=np.float32,
            )

        durations = result.pred_dur.detach().cpu().long()
        chars = [ch for ch in phonemes if model.vocab.get(ch) is not None]
        if durations.numel() != len(chars) + 2:
            raise RuntimeError(
                "nabra_duration_token_mismatch:"
                f"durations={durations.numel()}:chars={len(chars)}"
            )
        token_durations = durations[1:-1]

        cumulative_frames = [0]
        total = 0
        for value in token_durations.tolist():
            total += int(value)
            cumulative_frames.append(total)

        def sample_at(token_count: int) -> int:
            if token_count < 0 or token_count >= len(cumulative_frames):
                raise RuntimeError(f"nabra_invalid_token_mark:{token_count}")
            # Kokoro duration frames are 600 samples at 24 kHz (25 ms).
            return min(int(audio.size), cumulative_frames[token_count] * 600)

        marks: list[dict[str, Any]] = []
        previous_end = 0
        for index, item in enumerate(token_marks):
            start_sample = sample_at(int(item["start_token"]))
            speech_end_sample = sample_at(int(item["speech_end_token"]))
            pause_end_sample = sample_at(int(item["pause_end_token"]))
            if index == 0:
                start_sample = 0
            if index == len(token_marks) - 1:
                pause_end_sample = int(audio.size)
            start_sample = max(previous_end, start_sample)
            speech_end_sample = max(start_sample + 1, speech_end_sample)
            pause_end_sample = max(speech_end_sample + 1, pause_end_sample)
            pause_end_sample = min(int(audio.size), pause_end_sample)
            if pause_end_sample <= speech_end_sample:
                raise RuntimeError(
                    f"nabra_native_pause_has_no_duration:role={item['role']}"
                )
            marks.append(
                {
                    **item,
                    "start_sample": start_sample,
                    "speech_end_sample": speech_end_sample,
                    "pause_end_sample": pause_end_sample,
                    "start_seconds": round(start_sample / NABRA_SAMPLE_RATE, 6),
                    "speech_end_seconds": round(
                        speech_end_sample / NABRA_SAMPLE_RATE, 6
                    ),
                    "pause_end_seconds": round(
                        pause_end_sample / NABRA_SAMPLE_RATE, 6
                    ),
                    "native_pause_seconds": round(
                        (pause_end_sample - speech_end_sample) / NABRA_SAMPLE_RATE,
                        6,
                    ),
                }
            )
            previous_end = pause_end_sample

        try:
            import soundfile as sf
        except ImportError as exc:
            raise RuntimeError(f"nabra_audio_runtime_missing:{type(exc).__name__}") from exc

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_name(f".{output_path.name}.nabra.tmp.wav")
        try:
            sf.write(
                temporary,
                audio,
                NABRA_SAMPLE_RATE,
                subtype="PCM_16",
                format="WAV",
            )
            if not temporary.is_file() or temporary.stat().st_size < 1024:
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
            "single_continuous_inference": True,
            "external_silence_insertions": 0,
            "tempo_or_pitch_change": False,
            "native_pause_tokens": True,
            "phoneme_stream": phonemes,
            "parts": marks,
        }

    def synthesize(self, transcript: str, output_path: Path) -> Path:
        text = " ".join(str(transcript or "").split()).strip()
        result = self.synthesize_continuous(
            [{"role": "narration", "text": text}],
            output_path,
        )
        return Path(result["path"])
