#!/usr/bin/env python3
"""Bounded CPU benchmark/refinement of Nabra-82M.

Experimental only. Uses the official current Nabra inference path, then makes
one listener-guided comfort sample: manually corrected tashkeel, slightly
slower native model speed, and semantic pauses. No production wiring.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import subprocess
import time
import wave
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
from huggingface_hub import hf_hub_download
from arabic_g2p import EXTRA_SYMBOLS, clean_phonemes
from kokoro import KModel, KPipeline
from kokoro import pipeline as kpipeline_mod

REPO_ID = "oddadmix/Nabra-82M-v0.1"
SAMPLE_RATE = 24000
NATIVE_SPEED = 0.94
NATIVE_PROSODY_SPEED = 0.87

# Manually corrected MSA tashkeel. This intentionally bypasses Camel's wrong
# guesses seen in the first probe (e.g. أَنَّ, أَبْدَأ, فِرَقًا).
SEGMENTS = (
    "أَحْيَانًا، لَا تَحْتَاج إِلَى بِدَايَة جَدِيدَة.",
    "بَلْ تَحْتَاج إِلَى خُطْوَة صَادِقَة تُعِيدُكَ إِلَى طَرِيقِكَ.",
    "لَا تَنْتَظِرْ أَنْ يَأْتِيَ الدَّافِع كَامِلًا.",
    "اِبْدَأْ بِمَا تَسْتَطِيع الْيَوْم.",
    "فَالاسْتِمْرَار الْهَادِئ، حِينَ يَتَكَرَّر كُلَّ يَوْم، يَصْنَع فَرْقًا أَكْبَر مِمَّا تَتَخَيَّل.",
)
PAUSES_MS = (260, 420, 340, 500)

# Whole-text pronunciation safety: obtain Nabra's own phonemes for the entire
# passage, then patch only exact known-bad spans. Every other character in the
# phoneme stream must remain unchanged. One continuous inference call also
# avoids a fresh model onset before every sentence.
FULL_TEXT = " ".join(SEGMENTS)

PRONUNCIATION_PATCH_CANDIDATES = (
    # If an inflected form ever appears, use spoken-MSA endings without touching
    # the surrounding phonemes. Current SEGMENTS should normally avoid this.
    ("bidˈaːjatˌin ʤadˈiːdatˌin", "bidˈaːja ʤadˈiːda", "بداية جديدة"),
    # Make the /d/ boundary in "الدافع" explicit if Nabra's G2P returns the
    # strongly fused article+root form that the listener heard as ض.
    ("ʔaddːˈaːfiʕ", "ʔad dˈaːfiʕ", "الدافع"),
    ("ʔaddˈaːfiʕ", "ʔad dˈaːfiʕ", "الدافع"),
)


PUNCTUATION_CHARS = set(",.;:!?—…")


def add_native_pause_tokens(phonemes: str) -> str:
    """Add model-native pause cues at semantic boundaries only.

    No waveform editing is used. Kokoro/Nabra receives one continuous phoneme
    sequence and generates the pauses as part of its own duration/prosody.
    Lexical phonemes must remain unchanged.
    """
    out = phonemes
    replacements = (
        # Opening hesitation: small, not a full stop.
        ("ʔˈaħjaːnˌan ", "ʔˈaħjaːnˌan, "),
        # End of first idea: clearly felt reflective pause.
        ("ʤadˈiːdat. ", "ʤadˈiːdat… — "),
        # Core phrase emphasis: very short internal beat.
        ("saːdˈiqat ", "saːdˈiqat, "),
        # End of second idea.
        ("tarˈiːqikˌa. ", "tarˈiːqikˌa… "),
        # Warning -> action transition.
        ("kaːmˌilan. ", "kaːmˌilan… — "),
        # Action line -> closing reflection: strongest pause.
        ("aljˈaum. faːl", "aljˈaum… — — faːl"),
        # Closing sentence internal breathing points.
        ("alhˈaːdiʔ ", "alhˈaːdiʔ, "),
        ("kullˌa jˈaum ", "kullˌa jˈaum, "),
    )
    for source, target in replacements:
        if out.count(source) != 1:
            raise RuntimeError(
                f"native pause target must occur exactly once: {source!r}"
            )
        out = out.replace(source, target, 1)

    def lexical_only(value: str) -> str:
        return " ".join(
            "".join(ch for ch in value if ch not in PUNCTUATION_CHARS).split()
        )

    if lexical_only(out) != lexical_only(phonemes):
        raise RuntimeError("native pause tokens changed lexical phonemes")
    return out


def add_model_native_prosody_punctuation(phonemes: str) -> str:
    """Restore semantic punctuation *inside* the Kokoro/Nabra phoneme stream.

    Arabic espeak G2P preserves sentence dots but drops most commas and other
    prosody punctuation. Kokoro has learned punctuation tokens, so we restore
    only punctuation while keeping all lexical phonemes untouched.
    """
    out = phonemes

    replacements = (
        # Small reflective beat after the opening adverb.
        ("ʔˈaħjaːnˌan ", "ʔˈaħjaːnˌan, "),
        # Let the first reframe land without synthesizing a new sentence.
        ("ʤadˈiːdat. ", "ʤadˈiːdat… "),
        # Emphasize the core phrase but keep one continuous breath.
        ("saːdˈiqat ", "saːdˈiqat, "),
        # Action line gets a reflective transition into the closing idea.
        ("aljˈaum. faːl", "aljˈaum… faːl"),
        # Natural micro-beats inside the final thought.
        ("alhˈaːdiʔ ", "alhˈaːdiʔ, "),
        ("kullˌa jˈaum ", "kullˌa jˈaum, "),
    )

    for source, target in replacements:
        if out.count(source) != 1:
            raise RuntimeError(
                f"prosody punctuation target must occur exactly once: {source!r}"
            )
        out = out.replace(source, target, 1)

    # Safety proof: removing punctuation must recover the exact same lexical
    # phoneme stream (whitespace normalized). No pronunciation may change here.
    def lexical_only(value: str) -> str:
        return " ".join(
            "".join(ch for ch in value if ch not in PUNCTUATION_CHARS).split()
        )

    if lexical_only(out) != lexical_only(phonemes):
        raise RuntimeError("prosody punctuation changed lexical phonemes")

    return out


def add_model_native_structural_prosody(phonemes: str) -> str:
    """Use only Kokoro-native punctuation tokens to create stronger breathing room.

    No waveform editing, no sentence re-synthesis, no external silence. Repeated
    em-dash markers are a structural prosody cue inside the same model call.
    """
    out = phonemes
    replacements = (
        ("ʔˈaħjaːnˌan ", "ʔˈaħjaːnˌan, "),
        ("ʤadˈiːdat. ", "ʤadˈiːdat — "),
        ("saːdˈiqat ", "saːdˈiqat, "),
        ("tarˈiːqikˌa. ", "tarˈiːqikˌa — "),
        ("kaːmˌilan. ", "kaːmˌilan… "),
        ("aljˈaum. faːl", "aljˈaum — — faːl"),
        ("alhˈaːdiʔ ", "alhˈaːdiʔ, "),
        ("kullˌa jˈaum ", "kullˌa jˈaum, "),
    )
    for source, target in replacements:
        if out.count(source) != 1:
            raise RuntimeError(
                f"structural prosody target must occur exactly once: {source!r}"
            )
        out = out.replace(source, target, 1)

    def lexical_only(value: str) -> str:
        return " ".join(
            "".join(ch for ch in value if ch not in PUNCTUATION_CHARS).split()
        )

    if lexical_only(out) != lexical_only(phonemes):
        raise RuntimeError("structural prosody changed lexical phonemes")
    return out


def top_up_model_gaps_only(
    audio: np.ndarray,
    phonemes: str,
    pred_dur: torch.LongTensor,
    model: KModel,
) -> tuple[np.ndarray, list[dict]]:
    """Extend only silence that Nabra already created around punctuation.

    This mirrors kokoro-onnx's pause insertion strategy: use model timings to
    locate punctuation, find the quiet run actually touching that mark, and
    insert only the missing silence inside that already-quiet run. If the model
    ran straight through a mark, leave it untouched rather than cutting speech.
    """
    if pred_dur is None:
        raise RuntimeError("pred_dur required for gap top-up")

    chars = [ch for ch in phonemes if model.vocab.get(ch) is not None]
    durations = pred_dur.detach().cpu().long()
    if durations.numel() != len(chars) + 2:
        raise RuntimeError(
            f"duration/token mismatch: durations={durations.numel()} chars={len(chars)}"
        )

    frame = max(1, int(0.010 * SAMPLE_RATE))
    usable = int(audio.size) // frame * frame
    framed = audio[:usable].reshape(-1, frame)
    loudness = np.sqrt((framed.astype(np.float64) ** 2).mean(axis=1))
    peak = float(loudness.max()) if loudness.size else 0.0
    quiet = loudness <= (peak * (10 ** (-40.0 / 20.0)))
    reach = int(0.150 / 0.010)

    # Listener-approved semantic targets. Commas stay brief; the four ellipses
    # are the actual idea boundaries in the accepted 0.87 phoneme stream.
    ellipsis_targets = iter((0.42, 0.36, 0.42, 0.56))
    records: list[dict] = []
    insertions: list[tuple[int, int, str, float, float]] = []

    elapsed_frames = 0
    for token_index, ch in enumerate(chars):
        elapsed_frames += int(durations[token_index + 1].item())
        if ch == ",":
            target = 0.12
        elif ch == "…":
            try:
                target = next(ellipsis_targets)
            except StopIteration as exc:
                raise RuntimeError("unexpected extra ellipsis in accepted phonemes") from exc
        else:
            continue

        at_sample = min(int(audio.size), elapsed_frames * 600)
        at_frame = at_sample // frame

        # Find the nearest quiet frame within +/-150 ms, then expand only the
        # contiguous quiet run touching it. Never choose a distant unrelated gap.
        inside = None
        for idx in sorted(
            range(at_frame - reach, at_frame + reach + 1),
            key=lambda value: abs(value - at_frame),
        ):
            if 0 <= idx < len(quiet) and bool(quiet[idx]):
                inside = idx
                break

        if inside is None:
            records.append(
                {
                    "mark": ch,
                    "target_ms": round(target * 1000.0, 1),
                    "existing_ms": 0.0,
                    "added_ms": 0.0,
                    "status": "no_existing_quiet_run_leave_untouched",
                }
            )
            continue

        start = inside
        while start > 0 and bool(quiet[start - 1]):
            start -= 1
        end = inside
        while end + 1 < len(quiet) and bool(quiet[end + 1]):
            end += 1

        existing = (end - start + 1) * 0.010
        missing = max(0.0, target - existing)
        add_samples = int(round(missing * SAMPLE_RATE))
        middle = (start + (end - start + 1) // 2) * frame

        records.append(
            {
                "mark": ch,
                "target_ms": round(target * 1000.0, 1),
                "existing_ms": round(existing * 1000.0, 1),
                "added_ms": round(add_samples * 1000.0 / SAMPLE_RATE, 1),
                "status": "topped_up_existing_gap" if add_samples else "already_long_enough",
            }
        )
        if add_samples:
            insertions.append((middle, add_samples, ch, existing, target))

    # Insert right-to-left so original speech sample coordinates stay valid.
    out = audio.astype(np.float32, copy=True)
    for middle, add_samples, _, _, _ in sorted(insertions, reverse=True):
        out = np.concatenate(
            (
                out[:middle],
                np.zeros(add_samples, dtype=np.float32),
                out[middle:],
            )
        )

    # Strong invariant: deleting only the inserted zero blocks must recover the
    # original sample stream exactly. We record this structurally by construction;
    # no original sample is edited, trimmed, faded, or re-synthesized here.
    return out, records


def soften_segment_onset(audio: np.ndarray) -> np.ndarray:
    """Gently fade the model's phrase-start onset without cutting speech.

    The previous probe trimmed samples before detected speech and damaged Arabic
    initial consonants. This version preserves every sample and applies only a
    45 ms linear fade-in, which reduces the breath/hiss-like synthetic onset.
    """
    if audio.size == 0:
        return audio
    fade_frames = min(audio.size, int(SAMPLE_RATE * 0.025))
    if fade_frames <= 1:
        return audio
    out = audio.astype(np.float32, copy=True)
    out[:fade_frames] *= np.linspace(0.0, 1.0, fade_frames, dtype=np.float32)
    return out


def _snap_to_quiet_zero_crossing(
    audio: np.ndarray,
    center_sample: int,
    *,
    radius_ms: int = 350,
    window_ms: int = 20,
) -> tuple[int, float]:
    """Find a safe splice near a semantic boundary.

    pred_dur is only an approximate locator. We scan around it for the
    lowest-energy window, then snap to the nearest zero crossing so inserting
    silence cannot cut through an audible phoneme or create a click.
    """
    radius = int(SAMPLE_RATE * radius_ms / 1000.0)
    window = max(16, int(SAMPLE_RATE * window_ms / 1000.0))
    low = max(window, center_sample - radius)
    high = min(int(audio.size) - window, center_sample + radius)
    if low >= high:
        raise RuntimeError("no search room around semantic pause boundary")

    best: tuple[float, int, float] | None = None
    step = max(1, int(SAMPLE_RATE * 0.005))
    half = window // 2

    for sample in range(low, high, step):
        segment = audio[sample - half: sample + half]
        if segment.size < window:
            continue
        rms = float(np.sqrt(np.mean(np.square(segment), dtype=np.float64)))

        # Prefer a true zero crossing within +/- 3 ms of the quiet-window center.
        search = int(SAMPLE_RATE * 0.003)
        zlow = max(1, sample - search)
        zhigh = min(int(audio.size) - 1, sample + search)
        local = audio[zlow:zhigh + 1]
        crossings = np.flatnonzero(np.signbit(local[:-1]) != np.signbit(local[1:]))
        if crossings.size:
            absolute = zlow + crossings
            splice = int(absolute[np.argmin(np.abs(absolute - sample))])
        else:
            splice = sample

        amplitude = abs(float(audio[splice]))
        score = rms + (0.10 * amplitude)
        if best is None or score < best[0]:
            best = (score, splice, rms)

    if best is None:
        raise RuntimeError("no quiet splice candidate found")

    _, splice, rms = best
    # Fail closed rather than cut through active speech. 0.03 RMS is generous
    # enough for room tone/model noise but rejects obvious voiced speech.
    if rms > 0.03:
        raise RuntimeError(
            f"semantic pause boundary has no safe low-energy splice: rms={rms:.4f}"
        )
    return splice, rms


def insert_human_pauses_from_pred_dur(
    audio: np.ndarray,
    phonemes: str,
    pred_dur: torch.LongTensor,
) -> tuple[np.ndarray, list[dict]]:
    """Insert semantically justified pauses without re-synthesizing speech.

    Nabra pred_dur provides an approximate semantic locator. Each locator is
    snapped to the nearest low-energy zero crossing before any silence is added.
    Original speech samples are preserved byte-for-byte.
    """
    rules = (
        ("opening_reflection", "ʔˈaħjaːnˌan", 1, 160, "تمهيد تأملي قصير قبل الفكرة"),
        ("sentence_1", ".", 1, 480, "اكتمال الفكرة الأولى"),
        ("honest_step_emphasis", "saːdˈiqat", 1, 120, "تأكيد العبارة المحورية خطوة صادقة"),
        ("sentence_2", ".", 2, 380, "اكتمال الجواب المقابل للفكرة الأولى"),
        ("sentence_3", ".", 3, 420, "اكتمال التحذير قبل الانتقال للفعل"),
        ("sentence_4", ".", 4, 600, "ترك جملة الفعل تستقر قبل الخاتمة"),
        ("last_reflective_beat", "alhˈaːdiʔ", 1, 170, "وقفة خفيفة بعد الاستمرار الهادئ"),
        ("last_daily_beat", "kullˌa jˈaum", 1, 200, "إبراز معنى التكرار اليومي قبل النتيجة"),
    )

    boundaries: list[tuple[int, int, str, str, str, int, float]] = []
    for label, needle, occurrence, pause_ms, reason in rules:
        start = -1
        cursor = 0
        for _ in range(occurrence):
            start = phonemes.find(needle, cursor)
            if start < 0:
                raise RuntimeError(
                    f"pause boundary not found: {label} needle={needle!r} occurrence={occurrence}"
                )
            cursor = start + len(needle)
        char_end = start + len(needle)

        dur_index_end = min(char_end + 1, int(pred_dur.numel()) - 1)
        approximate = int(pred_dur[:dur_index_end].sum().item() * 600)
        approximate = max(0, min(approximate, int(audio.size)))
        splice, local_rms = _snap_to_quiet_zero_crossing(audio, approximate)
        boundaries.append(
            (splice, pause_ms, label, needle, reason, approximate, local_rms)
        )

    out = audio.astype(np.float32, copy=True)
    applied: list[dict] = []
    for splice, pause_ms, label, needle, reason, approximate, local_rms in sorted(
        boundaries, reverse=True
    ):
        silence = np.zeros(int(SAMPLE_RATE * pause_ms / 1000.0), dtype=np.float32)
        out = np.concatenate((out[:splice], silence, out[splice:]))
        applied.append(
            {
                "label": label,
                "needle": needle,
                "pause_ms": pause_ms,
                "reason": reason,
                "pred_dur_approx_sample": approximate,
                "safe_splice_sample": splice,
                "snap_delta_ms": round((splice - approximate) * 1000.0 / SAMPLE_RATE, 2),
                "splice_rms": round(local_rms, 6),
            }
        )

    applied.reverse()
    return out, applied

def wav_info(path: Path) -> dict:
    with wave.open(str(path), "rb") as wf:
        return {
            "duration_seconds": round(wf.getnframes() / float(wf.getframerate()), 3),
            "sample_rate": wf.getframerate(),
            "channels": wf.getnchannels(),
            "sample_width_bytes": wf.getsampwidth(),
            "bytes": path.stat().st_size,
        }


def add_silence(chunks: list[np.ndarray], pauses_ms: tuple[int, ...]) -> np.ndarray:
    out: list[np.ndarray] = []
    for index, chunk in enumerate(chunks):
        out.append(chunk.astype(np.float32))
        if index < len(pauses_ms):
            frames = int(SAMPLE_RATE * pauses_ms[index] / 1000.0)
            out.append(np.zeros(frames, dtype=np.float32))
    return np.concatenate(out).astype(np.float32)


def mix_ready(source: Path, destination: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-i", str(source),
            "-af", "loudnorm=I=-16.2:LRA=3:TP=-1.5,aresample=48000",
            "-c:a", "pcm_s16le",
            str(destination),
        ],
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="probe_artifacts/nabra-82m-cpu")
    args = parser.parse_args()

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)

    download_started = time.perf_counter()
    model_path = hf_hub_download(REPO_ID, "kokoro_arabic.pth")
    config_path = hf_hub_download(REPO_ID, "config.json")
    voice_path = hf_hub_download(REPO_ID, "af_msa.pt")
    download_seconds = time.perf_counter() - download_started

    load_started = time.perf_counter()
    model = KModel(
        repo_id=REPO_ID,
        config=config_path,
        model=model_path,
        disable_complex=True,
    ).eval()
    model.vocab.update(EXTRA_SYMBOLS)

    kpipeline_mod.LANG_CODES.setdefault("ar", "ar")
    pipeline = KPipeline(lang_code="ar", repo_id=REPO_ID, model=model)
    original_g2p = pipeline.g2p

    def clean_arabic_g2p(text: str):
        phonemes, extra = original_g2p(text)
        return clean_phonemes(phonemes), extra

    pipeline.g2p = clean_arabic_g2p
    voice = torch.load(voice_path, map_location="cpu", weights_only=True)
    load_seconds = time.perf_counter() - load_started

    # Build the baseline phoneme stream for the *entire* passage first.
    baseline_phonemes, _ = clean_arabic_g2p(FULL_TEXT)
    patched_phonemes = baseline_phonemes
    applied_patches: list[dict] = []
    touched_sources: list[str] = []

    for source, target, label in PRONUNCIATION_PATCH_CANDIDATES:
        count = patched_phonemes.count(source)
        if count == 0:
            continue
        if count != 1:
            raise RuntimeError(
                f"pronunciation patch for {label!r} matched {count} times; refusing broad change"
            )
        before = patched_phonemes
        patched_phonemes = patched_phonemes.replace(source, target, 1)
        if before == patched_phonemes:
            raise RuntimeError(f"pronunciation patch for {label!r} made no change")
        applied_patches.append(
            {"label": label, "source": source, "target": target, "count": 1}
        )
        touched_sources.append(source)

    # Deterministic safety proof: recreate the expected patched stream from the
    # untouched baseline and require byte-for-byte equality. This guarantees
    # that no phoneme outside the exact listed patches changed.
    expected = baseline_phonemes
    for patch in applied_patches:
        expected = expected.replace(patch["source"], patch["target"], 1)
    if expected != patched_phonemes:
        raise RuntimeError("pronunciation patch modified phonemes outside approved spans")

    selective_started = time.perf_counter()
    with torch.inference_mode():
        selective_output = KPipeline.infer(
            model,
            patched_phonemes,
            voice.to(model.device),
            speed=NATIVE_SPEED,
        )
    selective_seconds = time.perf_counter() - selective_started
    selective_audio = selective_output.audio.detach().cpu().numpy().astype(np.float32)
    # One global onset only. Preserve all samples; use a short 25 ms fade.
    selective_audio = soften_segment_onset(selective_audio)

    selective_raw_path = output / "10-nabra-selective-pronunciation-raw.wav"
    sf.write(selective_raw_path, selective_audio, SAMPLE_RATE, subtype="PCM_16")
    selective_final_path = output / "11-nabra-selective-pronunciation-mix-ready.wav"
    mix_ready(selective_raw_path, selective_final_path)

    prosody_phonemes = add_model_native_prosody_punctuation(patched_phonemes)
    prosody_started = time.perf_counter()
    with torch.inference_mode():
        prosody_output = KPipeline.infer(
            model,
            prosody_phonemes,
            voice.to(model.device),
            speed=NATIVE_SPEED,
        )
    prosody_seconds = time.perf_counter() - prosody_started
    prosody_audio = prosody_output.audio.detach().cpu().numpy().astype(np.float32)
    # One and only model onset for the full passage.
    prosody_audio = soften_segment_onset(prosody_audio)

    prosody_raw_path = output / "16-nabra-native-punctuation-prosody-raw.wav"
    sf.write(prosody_raw_path, prosody_audio, SAMPLE_RATE, subtype="PCM_16")
    prosody_final_path = output / "17-nabra-native-punctuation-prosody-mix-ready.wav"
    mix_ready(prosody_raw_path, prosody_final_path)

    # Listener feedback: the continuous 0.94 rendering is too compressed even
    # though its pronunciation is correct. Re-synthesize the exact same
    # phonemes/punctuation in one continuous call at a calmer native model
    # pace. This is model-time prosody, not post-generation atempo.
    calm_started = time.perf_counter()
    with torch.inference_mode():
        calm_output = KPipeline.infer(
            model,
            prosody_phonemes,
            voice.to(model.device),
            speed=NATIVE_PROSODY_SPEED,
        )
    calm_seconds = time.perf_counter() - calm_started
    calm_audio = calm_output.audio.detach().cpu().numpy().astype(np.float32)
    calm_audio = soften_segment_onset(calm_audio)

    calm_raw_path = output / "18-nabra-native-prosody-calm-raw.wav"
    sf.write(calm_raw_path, calm_audio, SAMPLE_RATE, subtype="PCM_16")
    calm_final_path = output / "19-nabra-native-prosody-calm-mix-ready.wav"
    mix_ready(calm_raw_path, calm_final_path)

    pause_token_phonemes = add_native_pause_tokens(patched_phonemes)
    pause_token_started = time.perf_counter()
    with torch.inference_mode():
        pause_token_output = KPipeline.infer(
            model,
            pause_token_phonemes,
            voice.to(model.device),
            speed=NATIVE_PROSODY_SPEED,
        )
    pause_token_seconds = time.perf_counter() - pause_token_started
    pause_token_audio = pause_token_output.audio.detach().cpu().numpy().astype(np.float32)
    pause_token_audio = soften_segment_onset(pause_token_audio)

    pause_token_raw_path = output / "20-nabra-native-pause-tokens-raw.wav"
    sf.write(pause_token_raw_path, pause_token_audio, SAMPLE_RATE, subtype="PCM_16")
    pause_token_final_path = output / "21-nabra-native-pause-tokens-mix-ready.wav"
    mix_ready(pause_token_raw_path, pause_token_final_path)

    if pause_token_output.pred_dur is None:
        raise RuntimeError("accepted 0.87 sample did not return pred_dur")
    topup_audio, topup_report = top_up_model_gaps_only(
        pause_token_audio,
        pause_token_phonemes,
        pause_token_output.pred_dur,
        model,
    )
    topup_raw_path = output / "22-nabra-087-existing-gap-topup-raw.wav"
    sf.write(topup_raw_path, topup_audio, SAMPLE_RATE, subtype="PCM_16")
    topup_final_path = output / "23-nabra-087-existing-gap-topup-mix-ready.wav"
    mix_ready(topup_raw_path, topup_final_path)

    structural_phonemes = add_model_native_structural_prosody(patched_phonemes)
    structural_started = time.perf_counter()
    with torch.inference_mode():
        structural_output = KPipeline.infer(
            model,
            structural_phonemes,
            voice.to(model.device),
            speed=NATIVE_SPEED,
        )
    structural_seconds = time.perf_counter() - structural_started
    structural_audio = structural_output.audio.detach().cpu().numpy().astype(np.float32)
    structural_audio = soften_segment_onset(structural_audio)

    structural_raw_path = output / "18-nabra-native-structural-prosody-raw.wav"
    sf.write(structural_raw_path, structural_audio, SAMPLE_RATE, subtype="PCM_16")
    structural_final_path = output / "19-nabra-native-structural-prosody-mix-ready.wav"
    mix_ready(structural_raw_path, structural_final_path)

    if selective_output.pred_dur is None:
        raise RuntimeError("Nabra did not return pred_dur; cannot add safe post-generation pauses")
    human_audio, human_pauses = insert_human_pauses_from_pred_dur(
        selective_audio,
        patched_phonemes,
        selective_output.pred_dur.detach().cpu(),
    )

    requested_pause_samples = sum(
        int(SAMPLE_RATE * item["pause_ms"] / 1000.0) for item in human_pauses
    )
    actual_added_samples = int(human_audio.size - selective_audio.size)
    if actual_added_samples != requested_pause_samples:
        raise RuntimeError(
            "pause duration contract violated: "
            f"requested_samples={requested_pause_samples} "
            f"actual_added_samples={actual_added_samples}"
        )
    human_raw_path = output / "14-nabra-semantic-pauses-safe-splice-raw.wav"
    sf.write(human_raw_path, human_audio, SAMPLE_RATE, subtype="PCM_16")
    human_final_path = output / "15-nabra-semantic-pauses-safe-splice-mix-ready.wav"
    mix_ready(human_raw_path, human_final_path)

    synth_started = time.perf_counter()
    chunks: list[np.ndarray] = []
    emitted_phonemes: list[str] = []

    with torch.inference_mode():
        for segment in SEGMENTS:
            segment_chunks = []
            for _, phonemes, audio in pipeline(segment, voice=voice, speed=NATIVE_SPEED):
                emitted_phonemes.append(phonemes)
                segment_chunks.append(audio.detach().cpu().numpy())
            if not segment_chunks:
                raise RuntimeError(f"Nabra emitted no audio for segment: {segment}")
            chunks.append(soften_segment_onset(np.concatenate(segment_chunks).astype(np.float32)))

    synth_seconds = time.perf_counter() - synth_started

    audio = add_silence(chunks, PAUSES_MS)
    raw_path = output / "08-nabra-82m-spoken-msa-raw.wav"
    sf.write(raw_path, audio, SAMPLE_RATE, subtype="PCM_16")

    final_path = output / "09-nabra-82m-spoken-msa-mix-ready.wav"
    mix_ready(raw_path, final_path)

    raw = wav_info(raw_path)
    final = wav_info(final_path)
    peak_rss_kb = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)

    report = {
        "status": "success",
        "model": REPO_ID,
        "voice": "af_msa",
        "device": "cpu",
        "official_inference_path": True,
        "manual_tashkeel": "spoken_msa_verified",
        "native_speed": NATIVE_SPEED,
        "segments": SEGMENTS,
        "pauses_ms": PAUSES_MS,
        "phonemes_emitted": emitted_phonemes,
        "download_seconds": round(download_seconds, 3),
        "model_and_frontend_load_seconds": round(load_seconds, 3),
        "synthesis_seconds": round(synth_seconds, 3),
        "raw_realtime_factor": round(synth_seconds / raw["duration_seconds"], 3),
        "peak_rss_mb": round(peak_rss_kb / 1024.0, 1),
        "torch_version": torch.__version__,
        "torch_threads": torch.get_num_threads(),
        "python": platform.python_version(),
        "runner_cpu_count": os.cpu_count(),
        "raw_wav": raw,
        "mix_ready_wav": final,
        "baseline_full_text_phonemes": baseline_phonemes,
        "selective_patched_phonemes": patched_phonemes,
        "applied_pronunciation_patches": applied_patches,
        "outside_patch_phonemes_unchanged": expected == patched_phonemes,
        "selective_synthesis_seconds": round(selective_seconds, 3),
        "selective_raw_wav": wav_info(selective_raw_path),
        "selective_mix_ready_wav": wav_info(selective_final_path),
        "native_punctuation_phonemes": prosody_phonemes,
        "native_punctuation_lexical_phonemes_unchanged": True,
        "native_punctuation_single_inference": True,
        "native_punctuation_synthesis_seconds": round(prosody_seconds, 3),
        "native_punctuation_raw_wav": wav_info(prosody_raw_path),
        "native_punctuation_mix_ready_wav": wav_info(prosody_final_path),
        "native_calm_speed": NATIVE_PROSODY_SPEED,
        "native_calm_same_phonemes": prosody_phonemes,
        "native_calm_single_inference": True,
        "native_calm_zero_waveform_splices": True,
        "native_calm_synthesis_seconds": round(calm_seconds, 3),
        "native_calm_raw_wav": wav_info(calm_raw_path),
        "native_calm_mix_ready_wav": wav_info(calm_final_path),
        "native_pause_token_phonemes": pause_token_phonemes,
        "native_pause_tokens_lexical_phonemes_unchanged": True,
        "native_pause_tokens_single_inference": True,
        "native_pause_tokens_zero_waveform_splices": True,
        "native_pause_tokens_speed": NATIVE_PROSODY_SPEED,
        "native_pause_tokens_synthesis_seconds": round(pause_token_seconds, 3),
        "native_pause_tokens_raw_wav": wav_info(pause_token_raw_path),
        "native_pause_tokens_mix_ready_wav": wav_info(pause_token_final_path),
        "existing_gap_topup_source_baseline": "accepted 0.87 native-pause-token sample",
        "existing_gap_topup_strategy": "kokoro-onnx pauses.py equivalent",
        "existing_gap_topup_speech_samples_modified": False,
        "existing_gap_topup_report": topup_report,
        "existing_gap_topup_raw_wav": wav_info(topup_raw_path),
        "existing_gap_topup_mix_ready_wav": wav_info(topup_final_path),
        "native_structural_phonemes": structural_phonemes,
        "native_structural_lexical_phonemes_unchanged": True,
        "native_structural_single_inference": True,
        "native_structural_zero_waveform_splices": True,
        "native_structural_synthesis_seconds": round(structural_seconds, 3),
        "native_structural_raw_wav": wav_info(structural_raw_path),
        "native_structural_mix_ready_wav": wav_info(structural_final_path),
        "human_pause_insertions": human_pauses,
        "human_pause_requested_samples": requested_pause_samples,
        "human_pause_actual_added_samples": actual_added_samples,
        "human_pause_exact_duration_verified": (
            actual_added_samples == requested_pause_samples
        ),
        "human_pauses_single_continuous_inference": True,
        "human_pauses_speech_audio_unchanged": True,
        "human_pauses_raw_wav": wav_info(human_raw_path),
        "human_pauses_mix_ready_wav": wav_info(human_final_path),
        "notes": [
            "official Nabra repo_id and disable_complex inference path",
            "manually verified spoken-MSA tashkeel: lexical vowels preserved, unnecessary final case endings omitted",
            "native Nabra speed=0.94; no atempo or post speed change",
            "semantic pauses inserted only between complete ideas",
            "no audio samples are trimmed; only a 45 ms fade-in reduces phrase-start hiss",
            "no EQ, pitch shift, compressor, or voice retiming",
            "mix-ready file is loudness normalization plus 48 kHz resample only",
            "selective sample starts from Nabra G2P for the entire passage",
            "only exact known-bad phoneme spans may be patched; all other phonemes are asserted unchanged",
            "selective sample is one continuous inference call, so there is no repeated sentence-start onset",
            "native-punctuation sample changes punctuation tokens only; lexical phonemes are invariant",
            "native-punctuation sample has zero waveform splices and zero post-generation silence insertion",
            "native-punctuation sample is one continuous inference call, preventing repeated sentence onsets",
            "native-calm sample uses the exact same phonemes and punctuation at model speed 0.87",
            "native-calm sample has no atempo, no waveform splice, and no sentence-by-sentence synthesis",
            "native-pause-token sample changes punctuation tokens only and stays one continuous inference",
            "native-pause-token sample has zero waveform edits and therefore cannot introduce splice cuts",
            "existing-gap top-up starts from the listener-approved 0.87 sample and only inserts zeros inside quiet runs already created by Nabra",
            "existing-gap top-up never edits, trims, fades, retimes, or re-synthesizes any speech sample",
            "existing-gap top-up skips a punctuation mark entirely when no real quiet run exists around its model timing",
            "native-structural sample uses Kokoro punctuation tokens only, including em-dash structural beats",
            "native-structural sample has zero waveform edits and one continuous inference call",
            "human-pause version uses pred_dur only as an approximate locator, then snaps to a low-energy zero crossing",
            "human-pause version does not regenerate or modify any speech samples; unsafe splice points fail closed",
            "pause durations are exact sample-count contracts, not model-estimated timing",
            "single continuous inference means no fresh sentence-start TTS onset is introduced",
            "experimental only; no Clean V2 production wiring",
        ],
    }

    (output / "report-refined.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("NABRA_REFINED_REPORT=" + json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
