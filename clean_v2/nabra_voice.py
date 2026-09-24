from __future__ import annotations

from pathlib import Path


NABRA_REPO_ID = "oddadmix/Nabra-82M-v0.1"
NABRA_VOICE = "af_msa"
NABRA_SPEED = 0.94
NABRA_SAMPLE_RATE = 24000


class NabraVoiceSynthesizer:
    """Small local Nabra-82M adapter used only after Charon is unavailable.

    Heavy ML dependencies and model files are loaded lazily, so a healthy Charon
    production run pays no model-download or RAM cost.
    """

    def __init__(self) -> None:
        self._runtime: tuple[object, object, object] | None = None

    def _load(self) -> tuple[object, object, object]:
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
        self._runtime = (pipeline, voice, torch)
        return self._runtime

    def synthesize(self, transcript: str, output_path: Path) -> Path:
        text = " ".join(str(transcript or "").split()).strip()
        if not text:
            raise RuntimeError("nabra_empty_transcript")
        if __import__("re").search(r"(?m)^\s*[AB]:\s*\S", text):
            raise RuntimeError("nabra_dialogue_not_supported")

        pipeline, voice, torch = self._load()
        chunks = []
        with torch.inference_mode():
            for _graphemes, _phonemes, audio in pipeline(
                text,
                voice=voice,
                speed=NABRA_SPEED,
            ):
                chunks.append(audio.detach().cpu().numpy())

        if not chunks:
            raise RuntimeError("nabra_no_audio")

        try:
            import numpy as np
            import soundfile as sf
        except ImportError as exc:
            raise RuntimeError(f"nabra_audio_runtime_missing:{type(exc).__name__}") from exc

        waveform = np.concatenate(chunks).astype(np.float32)
        if waveform.size < 256:
            raise RuntimeError("nabra_audio_too_short")

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_name(f".{output_path.name}.nabra.tmp.wav")
        try:
            sf.write(
                temporary,
                waveform,
                NABRA_SAMPLE_RATE,
                subtype="PCM_16",
                format="WAV",
            )
            if not temporary.is_file() or temporary.stat().st_size < 1024:
                raise RuntimeError("nabra_output_missing_or_empty")
            temporary.replace(output_path)
        finally:
            temporary.unlink(missing_ok=True)
        return output_path
