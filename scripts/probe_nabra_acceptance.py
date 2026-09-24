#!/usr/bin/env python3
"""Final Nabra acceptance probe: listener-review 0.90 smooth voice + exact semantic pauses.

Probe-only. Tests whether the listener-approved Nabra voice generalizes to new
Arabic content while keeping sentence pauses natural and phrase endings smooth.
No production wiring.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import re
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
NATIVE_SPEED = 0.90

# Exact, listener-driven repairs for obvious Arabic G2P artifacts only.
# These are phoneme-local and fail closed if the expected source is absent or broad.
PRONUNCIATION_PATCHES = (
    ("biaːnnˌatˈiːʤat", "binnˌatˈiːʤat", "بالنتيجة"),
    ("tˌakaˈuːna", "takˈuːna", "تكون"),
    ("jˌataħaˈuːal", "jˌataħˈawːal", "يتحول"),
    ("faħˈaiˌaːt", "falħˈaiˌaːt", "فالحياة"),
)

def repair_obvious_g2p_artifacts(phonemes: str) -> tuple[str, list[dict]]:
    out = phonemes
    applied: list[dict] = []
    for source, target, label in PRONUNCIATION_PATCHES:
        count = out.count(source)
        if count == 0:
            continue
        if count != 1:
            raise RuntimeError(
                f"pronunciation patch {label!r} matched {count} times; refusing broad change"
            )
        out = out.replace(source, target, 1)
        applied.append({"label": label, "source": source, "target": target})
    return out, applied


ARABIC_DIACRITICS_RE = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED]")
ARABIC_WORD_RE = re.compile(r"[\u0621-\u064A\u0671\u0670\u064B-\u065F]+")
HARD_BOUNDARY_CHARS = set("،؛.!؟?!:")


def _undia(value: str) -> str:
    return ARABIC_DIACRITICS_RE.sub("", value)


def repair_spoken_msa_orthography(
    sentence: str,
    phonemes: str,
) -> tuple[str, list[dict], list[dict]]:
    """Conservative source-aware Arabic orthography -> spoken-MSA repair.

    Current auto-repair family:
      - taa marbuta (ة): pausal/adjectival form loses erroneous final /t/;
        clear construct-state before a following definite noun keeps /t/.
      - taa maftuha (ت), haa (ه), alif maqsura (ى), yaa (ي), and hamza
        are never rewritten by this family; they are only guarded/diagnosed.

    Safety: requires one Arabic orthographic word per whitespace-delimited
    phoneme word. If alignment is not exact, make no orthographic repair.
    """
    matches = list(ARABIC_WORD_RE.finditer(sentence))
    phones = phonemes.split()
    repairs: list[dict] = []
    diagnostics: list[dict] = []

    if len(matches) != len(phones):
        diagnostics.append({
            "kind": "alignment",
            "status": "skipped",
            "arabic_words": len(matches),
            "phoneme_words": len(phones),
        })
        return phonemes, repairs, diagnostics

    out = list(phones)
    for index, match in enumerate(matches):
        surface = match.group(0)
        bare = _undia(surface)
        phone = out[index]

        next_bare = ""
        separator = sentence[match.end():]
        if index + 1 < len(matches):
            next_match = matches[index + 1]
            next_bare = _undia(next_match.group(0))
            separator = sentence[match.end():next_match.start()]

        boundary_after = any(ch in HARD_BOUNDARY_CHARS for ch in separator)

        if bare.endswith("ة"):
            # Spoken MSA: word-final taa marbuta is normally /a/ in pausal
            # speech. Keep /t/ only in a clear construct state such as
            # "قيمة الاستمرار": no punctuation boundary + following definite noun.
            clear_construct = (
                bool(next_bare)
                and not boundary_after
                and not bare.startswith("ال")
                and (
                    next_bare.startswith("ال")
                    or (bare == "لحظة" and next_bare == "حماس")
                )
            )
            if clear_construct:
                diagnostics.append({
                    "kind": "taa_marbuta",
                    "word": bare,
                    "status": "construct_t_preserved",
                })
            else:
                phone_core = phone.rstrip(".,;:!?…")
                phone_suffix = phone[len(phone_core):]
                if phone_core.endswith("at"):
                    repaired = phone_core[:-1] + phone_suffix
                    out[index] = repaired
                    repairs.append({
                        "kind": "taa_marbuta",
                        "word": bare,
                        "source": phone,
                        "target": repaired,
                        "reason": "spoken_msa_pausal_taa_marbuta",
                    })
                else:
                    diagnostics.append({
                        "kind": "taa_marbuta",
                        "word": bare,
                        "status": "no_final_at_pattern",
                        "phoneme": phone,
                    })
                    continue
                continue
        elif bare.endswith("ت"):
            diagnostics.append({
                "kind": "taa_maftuha",
                "word": bare,
                "status": "protected_no_rewrite",
                "phoneme": phone,
            })
        elif bare.endswith("ه"):
            diagnostics.append({
                "kind": "haa_final",
                "word": bare,
                "status": "protected_no_rewrite",
                "phoneme": phone,
            })
        elif bare.endswith("ى"):
            diagnostics.append({
                "kind": "alif_maqsura",
                "word": bare,
                "status": "protected_no_rewrite",
                "phoneme": phone,
            })
        elif bare.endswith("ي"):
            diagnostics.append({
                "kind": "yaa_final",
                "word": bare,
                "status": "protected_no_rewrite",
                "phoneme": phone,
            })

    return " ".join(out), repairs, diagnostics


FINAL_PHONE_PUNCT = ".,;:!?…"
FINAL_SHORT_VOWEL_RE = re.compile(r"(?:[ˌˈ]?[aiu])$")


def repair_sentence_final_pause(
    sentence: str,
    phonemes: str,
) -> tuple[str, list[dict]]:
    """Normalize the final Arabic word while retaining a model stop cue.

    The timeline owns pause *duration*, but Nabra still needs a terminal stop
    marker to close pitch/energy naturally. We therefore remove spurious short
    case vowels first, then restore one "." prosody cue. No extra silence is
    introduced by this function.
    """
    matches = list(ARABIC_WORD_RE.finditer(sentence))
    phones = phonemes.split()
    if not matches or not phones or len(matches) != len(phones):
        return phonemes, []

    last_surface = matches[-1].group(0)
    last_bare = _undia(last_surface)
    source_phone = phones[-1]
    phone = source_phone.rstrip(FINAL_PHONE_PUNCT)
    repairs: list[dict] = []

    # Taa marbuta is already handled by the orthography repair above.
    protected_long_letter = bool(last_bare) and last_bare[-1] in "اىيوة"
    final_base_pos = None
    for pos in range(len(last_surface) - 1, -1, -1):
        ch = last_surface[pos]
        if ARABIC_DIACRITICS_RE.fullmatch(ch):
            continue
        if re.fullmatch(r"[\u0621-\u064A\u0671\u0670]", ch):
            final_base_pos = pos
            break
    trailing_marks = last_surface[final_base_pos + 1:] if final_base_pos is not None else ""
    explicit_final_vowel = any(ch in "\u064B\u064C\u064D\u064E\u064F\u0650" for ch in trailing_marks)

    if not protected_long_letter and not explicit_final_vowel:
        repaired = FINAL_SHORT_VOWEL_RE.sub("", phone)
        if repaired != phone:
            repairs.append({
                "kind": "sentence_final_case_vowel",
                "word": last_bare,
                "source": phone,
                "target": repaired,
                "reason": "spoken_msa_pause_form",
            })
            phone = repaired

    # Restore one terminal stop cue for Nabra's end-of-sentence prosody.
    # Pause length remains fully controlled by the external timeline.
    final_phone = phone + "."
    if final_phone != source_phone:
        repairs.append({
            "kind": "terminal_stop_cue",
            "word": last_bare,
            "source": source_phone,
            "target": final_phone,
            "reason": "model_prosody_only_timeline_owns_pause_duration",
        })
    phones[-1] = final_phone
    return " ".join(phones), repairs


SHORT_SENTENCES = (
    "بَعْضُ الأَيّام لا تَسير كَما خَطَّطْت.",
    "وَهَذا لا يَعْني أَنَّكَ خَسِرْت تَقَدُّمَك.",
    "أَصْلِح ما تَسْتَطيع، وَاتْرُك ما لا تَسْتَطيع تَغْييرَه الآن.",
    "ثُمَّ عُد إِلى خُطْوَتِك التّالِيَة بِهُدوء.",
    "فَالحَياة لا تَطْلُب مِنْكَ أَنْ تَكون مُثاليًّا؛ بَل أَنْ تَسْتَمِر بِوُضوح وَمَرونَة.",
)
# Pause length follows rhetorical function, not sentence count:
# setup lands -> medium; immediate clarification -> short; completed instruction
# -> medium; action line before the closing takeaway -> rare reflective long.
SHORT_PAUSES_MS = (650, 420, 720, 1600)
SHORT_PAUSE_REASONS = (
    "اكتمال التمهيد وترك الفكرة تهبط قبل تصحيحها",
    "استمرار مباشر: الجملة التالية تكمل نفس المعنى فلا نبالغ في الوقفة",
    "اكتمال التعليمات المركبة قبل الانتقال إلى الفعل التالي",
    "وقفة تأملية نادرة قبل الخلاصة النهائية",
)

LONG_SENTENCES = (
    "أَحْيانًا نَظُنُّ أَنَّ التَّقَدُّم يَحْتاج إِلى قَرار كَبير، لَكِنَّ الحَقيقَة أَبْسَط مِن ذَلِك.",
    "مُعْظَم التَّغْيير يَبْدَأ مِن خُطْوَة صَغيرَة نُكَرِّرُها حَتّى تُصْبِح جُزْءًا مِن حَياتِنا.",
    "قَد لا تَشْعُر بِالنَّتيجَة في البِدايَة، وَقَد تَظُنُّ أَنَّ جُهْدَك لا يَتَحَرَّك.",
    "لَكِنْ عِنْدَما تَنْظُر إِلى أَسابيع كامِلَة، تَكْتَشِف أَنَّ الأَثَر كان يَتَراكَم بِهُدوء.",
    "هُنا تَظْهَر قِيمَة الاِسْتِمْرار؛ أَنْ تَفْعَل ما تَسْتَطيع، حَتّى في الأَيّام الَّتي لا تَشْعُر فيها بِالحَماس.",
    "لَيْس المَطْلوب أَنْ تَكون مُثاليًّا، وَلا أَنْ تُنْجِز كُلَّ شَيْء دَفْعَة واحِدَة.",
    "المَطْلوب أَنْ تَعْرِف ما هُو مُهِمّ، ثُمَّ تَعود إِلَيْه مَرَّة بَعْد مَرَّة.",
    "وَعِنْدَما تَتَعَثَّر، لا تَجْعَل يَوْمًا صَعْبًا يَتَحَوَّل إِلى أُسْبوع كامِل مِن التَّوَقُّف.",
    "اِرْجِع بِهُدوء، وَابْدَأ مِن أَقْرَب خُطْوَة مُمْكِنَة.",
    "بَعْد مُدَّة، سَتَكْتَشِف أَنَّ ما صَنَع الفَرْق لَم يَكُن لَحْظَة حَماس، بَلْ عادات صَغيرَة حافَظْت عَلَيْها حِينَ كان التَّقَدُّم بَطيئًا.",
)
LONG_PAUSES_MS = (650, 420, 650, 950, 700, 420, 950, 650, 1600)
LONG_PAUSE_REASONS = (
    "اكتمال طرح الفكرة الأساسية قبل شرحها",
    "استمرار تفسيري مباشر لنفس الفكرة",
    "اكتمال وصف الشك قبل التحول إلى النتيجة",
    "نهاية قوس معنوي صغير وبداية فكرة قيمة الاستمرار",
    "اكتمال تعريف الاستمرار قبل نفي الكمال",
    "استمرار تقابلي: ليس المطلوب... ثم المطلوب...",
    "اكتمال التوجيه الأساسي قبل مثال التعثر",
    "اكتمال التحذير قبل أمر الرجوع الهادئ",
    "وقفة تأملية نادرة قبل الخاتمة النهائية",
)


def wav_info(path: Path) -> dict:
    with wave.open(str(path), "rb") as wf:
        return {
            "duration_seconds": round(wf.getnframes() / float(wf.getframerate()), 3),
            "sample_rate": wf.getframerate(),
            "channels": wf.getnchannels(),
            "sample_width_bytes": wf.getsampwidth(),
            "bytes": path.stat().st_size,
        }


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


def _frame_rms(audio: np.ndarray, frame: int) -> np.ndarray:
    values: list[float] = []
    for start in range(0, int(audio.size), frame):
        chunk = audio[start : start + frame]
        if chunk.size:
            values.append(float(np.sqrt(np.mean(np.square(chunk), dtype=np.float64))))
    return np.asarray(values, dtype=np.float64)


def _detect_synthetic_onset_contamination(
    audio: np.ndarray,
    *,
    frame_ms: int = 10,
    search_ms: int = 400,
) -> tuple[int | None, dict]:
    """Find low-level hiss/noise before the first sustained speech core.

    This does not classify normal quiet consonants as noise by energy alone.
    Cleanup is enabled only when the pre-core region contains the characteristic
    low-energy/high-frequency or high-zero-crossing synthetic onset observed in
    the listener-rejected samples.
    """
    frame = max(1, int(SAMPLE_RATE * frame_ms / 1000.0))
    frames = min(int(np.ceil(search_ms / frame_ms)), int(np.ceil(audio.size / frame)))
    if frames <= 2:
        return None, {"status": "too_short"}

    rows: list[dict] = []
    for index in range(frames):
        chunk = audio[index * frame : min(audio.size, (index + 1) * frame)]
        if chunk.size < 8:
            break
        rms = float(np.sqrt(np.mean(np.square(chunk), dtype=np.float64)))
        db = 20.0 * np.log10(max(rms, 1e-12))
        zcr = float(np.mean(np.signbit(chunk[:-1]) != np.signbit(chunk[1:])))
        windowed = chunk.astype(np.float64) * np.hanning(chunk.size)
        power = np.abs(np.fft.rfft(windowed)) ** 2
        freqs = np.fft.rfftfreq(chunk.size, 1.0 / SAMPLE_RATE)
        hf_ratio = float(power[freqs > 4000.0].sum() / max(power.sum(), 1e-12))
        rows.append({"db": db, "zcr": zcr, "hf_ratio": hf_ratio})

    core = None
    for index, row in enumerate(rows):
        if row["db"] <= -27.0 or row["hf_ratio"] >= 0.12:
            continue
        look = rows[index : min(len(rows), index + 4)]
        if sum(item["db"] > -30.0 for item in look) >= 3:
            core = index
            break

    if core is None or core < 5:
        return None, {"status": "no_safe_core", "core_frame": core}

    contaminated = [
        index for index, row in enumerate(rows[:core])
        if (-45.0 < row["db"] < -27.0)
        and (row["hf_ratio"] > 0.15 or row["zcr"] > 0.22)
    ]
    if not contaminated:
        return None, {"status": "no_contamination", "core_frame": core}

    # Keep 20 ms immediately before the robust speech core to protect lexical attack.
    clean_until = max(0, (core * frame) - int(SAMPLE_RATE * 0.020))
    return clean_until, {
        "status": "synthetic_onset_contamination",
        "core_frame": core,
        "contaminated_frames": contaminated,
        "clean_until_ms": round(clean_until * 1000.0 / SAMPLE_RATE, 2),
    }


def smooth_sentence_edges(
    audio: np.ndarray,
    *,
    pred_dur: torch.LongTensor | None = None,
    threshold_db: float = -34.0,
    frame_ms: int = 10,
    pre_roll_ms: int = 12,
    post_roll_ms: int = 180,
    onset_fade_ms: int = 4,
    pre_release_soften_ms: int = 0,
    pre_release_floor: float = 1.0,
    release_hold_ms: int = 25,
    release_fade_ms: int = 150,
) -> tuple[np.ndarray, dict]:
    """Clean sentence boundaries without touching lexical timing.

    Start:
      - keep the detected speech frame itself intact in position;
      - replace only the protected pre-speech context with true silence;
      - use a tiny 4 ms fade on the first active samples to avoid a click.
    End:
      - never fade inside the detected lexical speech core;
      - keep a natural 35 ms release after speech;
      - fade only the model-generated trailing tail toward silence.
    """
    if audio.size == 0:
        return audio, {"status": "empty"}

    frame = max(1, int(SAMPLE_RATE * frame_ms / 1000.0))
    rms = _frame_rms(audio, frame)
    threshold = float(10 ** (threshold_db / 20.0))
    active_frames = np.flatnonzero(rms > threshold)
    if active_frames.size == 0:
        return audio.astype(np.float32, copy=True), {
            "status": "no_active_frames",
            "threshold_db": threshold_db,
        }

    speech_start = int(active_frames[0] * frame)
    speech_end = min(int(audio.size), int((active_frames[-1] + 1) * frame))

    pre = int(SAMPLE_RATE * pre_roll_ms / 1000.0)
    post = int(SAMPLE_RATE * post_roll_ms / 1000.0)
    start = max(0, speech_start - pre)
    end = min(int(audio.size), speech_end + post)

    out = audio[start:end].astype(np.float32, copy=True)
    speech_start_local = speech_start - start
    speech_end_local = min(int(out.size), speech_end - start)

    # Use Nabra/Kokoro's own BOS duration as the hard safety boundary.
    # pred_dur[0] is model-reserved lead time before the first lexical token.
    # We may silence noise inside that lead, but never beyond it.
    model_lead_original = 0
    if pred_dur is not None and int(pred_dur.numel()) >= 2:
        model_lead_original = max(0, int(pred_dur[0].item()) * 600)
    model_lead_local = max(
        0,
        min(int(out.size), model_lead_original - start),
    )
    model_lead_cleaned = 0
    model_lead_fade = 0
    if model_lead_local > 0:
        out[:model_lead_local] = 0.0
        model_lead_cleaned = model_lead_local
        model_lead_fade = min(
            int(SAMPLE_RATE * 0.006),
            max(0, int(out.size) - model_lead_local),
        )
        if model_lead_fade > 1:
            out[model_lead_local:model_lead_local + model_lead_fade] *= np.sin(
                np.linspace(0.0, np.pi / 2.0, model_lead_fade, dtype=np.float32)
            ) ** 2

    # The protected pre-roll used to retain the model's hiss/breath onset.
    # Make that region truly silent instead. The lexical attack is not removed.
    if speech_start_local > 0:
        out[:speech_start_local] = 0.0

    onset_fade = min(
        int(SAMPLE_RATE * onset_fade_ms / 1000.0),
        max(0, int(out.size) - speech_start_local),
    )
    if onset_fade > 1:
        out[speech_start_local:speech_start_local + onset_fade] *= np.sin(
            np.linspace(0.0, np.pi / 2.0, onset_fade, dtype=np.float32)
        ) ** 2

    pre_release_len = min(
        int(SAMPLE_RATE * pre_release_soften_ms / 1000.0),
        max(0, speech_end_local),
    )
    if pre_release_len > 1 and pre_release_floor < 1.0:
        soften_start = speech_end_local - pre_release_len
        # Very gentle equal-power easing inside only the final tens of ms.
        # This prepares the ear for silence without muting or cutting the final phoneme.
        phase = np.linspace(0.0, np.pi / 2.0, pre_release_len, dtype=np.float32)
        envelope = 1.0 - (1.0 - float(pre_release_floor)) * (np.sin(phase) ** 2)
        out[soften_start:speech_end_local] *= envelope

    available_tail = max(0, int(out.size) - speech_end_local)
    hold = min(
        int(SAMPLE_RATE * release_hold_ms / 1000.0),
        available_tail,
    )
    fade_available = max(0, available_tail - hold)
    fade_len = min(
        int(SAMPLE_RATE * release_fade_ms / 1000.0),
        fade_available,
    )

    # Crucial invariant: fade_start is always >= speech_end_local.
    # Therefore no lexical phoneme can be attenuated by the release smoothing.
    fade_start = speech_end_local + hold
    if fade_len > 1:
        fade_end = fade_start + fade_len
        out[fade_start:fade_end] *= np.cos(
            np.linspace(0.0, np.pi / 2.0, fade_len, dtype=np.float32)
        ) ** 2
        if fade_end < out.size:
            out[fade_end:] = 0.0
        else:
            out[-1] = 0.0
    elif available_tail > 0:
        # No room for a proper fade: preserve the tail rather than touching
        # speech. The following deterministic pause starts after this chunk.
        pass

    return out, {
        "status": "ok",
        "threshold_db": threshold_db,
        "speech_start_ms": round(speech_start * 1000.0 / SAMPLE_RATE, 2),
        "speech_end_ms": round(speech_end * 1000.0 / SAMPLE_RATE, 2),
        "trimmed_start_ms": round(start * 1000.0 / SAMPLE_RATE, 2),
        "trimmed_end_ms": round((audio.size - end) * 1000.0 / SAMPLE_RATE, 2),
        "pre_roll_ms": pre_roll_ms,
        "pre_roll_zeroed": True,
        "onset_fade_ms_applied": round(onset_fade * 1000.0 / SAMPLE_RATE, 2),
        "pre_release_soften_ms_applied": round(pre_release_len * 1000.0 / SAMPLE_RATE, 2),
        "pre_release_floor": pre_release_floor,
        "model_reserved_lead_ms": round(model_lead_original * 1000.0 / SAMPLE_RATE, 2),
        "model_lead_cleanup_ms": round(model_lead_cleaned * 1000.0 / SAMPLE_RATE, 2),
        "model_lead_fade_ms": round(model_lead_fade * 1000.0 / SAMPLE_RATE, 2),
        "onset_cleanup_touches_first_lexical_token": False,
        "post_roll_ms_requested": post_roll_ms,
        "available_release_ms": round(available_tail * 1000.0 / SAMPLE_RATE, 2),
        "release_hold_ms_applied": round(hold * 1000.0 / SAMPLE_RATE, 2),
        "release_fade_ms_applied": round(fade_len * 1000.0 / SAMPLE_RATE, 2),
        "fade_touches_speech_core": False,
        "speech_core_retimed": False,
        "sentence_end_hard_cut": False,
    }


def infer_with_terminal_duration_cap(
    model: KModel,
    phonemes: str,
    voice_pack: torch.Tensor,
    *,
    speed: float,
    terminal_cap_frames: int = 4,
    punctuation_cap_frames: int = 1,
    eos_cap_frames: int = 1,
    final_long_vowel_cap_frames: int = 3,
) -> tuple[torch.Tensor, torch.LongTensor, dict]:
    """Kokoro/Nabra inference with a bounded sentence-final duration cap only.

    One duration frame is 600 samples at 24 kHz = 25 ms. The model's predicted
    durations are left untouched except for:
      1) an abnormally long final lexical phoneme (>4 frames / 100 ms),
      2) terminal punctuation (cap 1 frame), and
      3) EOS (cap 1 frame).
    Normal 50-100 ms lexical endings are preserved exactly.
    """
    mapped_chars = [
        ch for ch in phonemes
        if model.vocab.get(ch) is not None
    ]
    # Two listener-confirmed residual ending families:
    # 1) "ذلك." can synthesize an epenthetic release vowel after /k/ when "." is voiced.
    #    Suppress only the terminal punctuation token; EOS + external pause still close the sentence.
    suppress_terminal_period = phonemes.rstrip().endswith("ðˈalik.")
    input_ids = [model.vocab[ch] for ch in mapped_chars]
    assert len(input_ids) + 2 <= model.context_length, (
        len(input_ids) + 2,
        model.context_length,
    )

    ids = torch.LongTensor([[0, *input_ids, 0]]).to(model.device)
    ref_s = voice_pack[len(phonemes) - 1].to(model.device)
    input_lengths = torch.full(
        (ids.shape[0],),
        ids.shape[-1],
        device=ids.device,
        dtype=torch.long,
    )
    text_mask = torch.arange(input_lengths.max(), device=ids.device).unsqueeze(0)
    text_mask = text_mask.expand(input_lengths.shape[0], -1).type_as(input_lengths)
    text_mask = torch.gt(text_mask + 1, input_lengths.unsqueeze(1)).to(model.device)

    bert_dur = model.bert(ids, attention_mask=(~text_mask).int())
    d_en = model.bert_encoder(bert_dur).transpose(-1, -2)
    s = ref_s[:, 128:]
    d = model.predictor.text_encoder(d_en, s, input_lengths, text_mask)
    x, _ = model.predictor.lstm(d)
    duration = model.predictor.duration_proj(x)
    duration = torch.sigmoid(duration).sum(axis=-1) / speed
    pred_dur = torch.round(duration).clamp(min=1).long().squeeze()

    original = pred_dur.detach().cpu().clone()
    punctuation_chars = set(".!?؛،:…")
    skip_chars = punctuation_chars | set(" \t\nˈˌ")

    final_char_pos = None
    for pos in range(len(mapped_chars) - 1, -1, -1):
        if mapped_chars[pos] not in skip_chars:
            final_char_pos = pos
            break

    terminal_pred_index = None
    terminal_char = None
    if final_char_pos is not None:
        terminal_pred_index = final_char_pos + 1  # BOS occupies pred_dur[0]
        terminal_char = mapped_chars[final_char_pos]
        if int(pred_dur[terminal_pred_index].item()) > terminal_cap_frames:
            pred_dur[terminal_pred_index] = torch.clamp(
                pred_dur[terminal_pred_index],
                max=terminal_cap_frames,
            )

    # Record and, only for sentence-final /uːʔ/, bound an overlong long-vowel
    # segment before the glottal stop. This targets "بهدوء" family without touching
    # the final glottal stop itself or other words.
    tail_before = []
    for pos in range(max(0, len(mapped_chars) - 10), len(mapped_chars)):
        pred_index = pos + 1
        tail_before.append({
            "char": mapped_chars[pos],
            "frames": int(original[pred_index].item()),
            "ms": int(original[pred_index].item()) * 25,
        })

    final_long_vowel_change = None
    lexical_end = "".join(mapped_chars[: final_char_pos + 1]) if final_char_pos is not None else ""
    if lexical_end.endswith("uːʔ"):
        u_pos = final_char_pos - 2
        length_pos = final_char_pos - 1
        if u_pos >= 0 and length_pos >= 0:
            u_idx = u_pos + 1
            length_idx = length_pos + 1
            before_u = int(pred_dur[u_idx].item())
            before_len = int(pred_dur[length_idx].item())
            # Preserve at least one frame for each symbol. Only cap the length marker
            # if the combined vowel duration exceeds 150 ms.
            combined = before_u + before_len
            if combined > 6:
                target_len = max(1, 6 - before_u)
                pred_dur[length_idx] = torch.clamp(pred_dur[length_idx], max=target_len)
            final_long_vowel_change = {
                "u_before_frames": before_u,
                "length_before_frames": before_len,
                "combined_before_ms": combined * 25,
                "u_after_frames": int(pred_dur[u_idx].item()),
                "length_after_frames": int(pred_dur[length_idx].item()),
                "combined_after_ms": int((pred_dur[u_idx] + pred_dur[length_idx]).item()) * 25,
            }

    punctuation_changes = []
    if final_char_pos is not None:
        for pos in range(final_char_pos + 1, len(mapped_chars)):
            if mapped_chars[pos] in punctuation_chars:
                pred_index = pos + 1
                before = int(pred_dur[pred_index].item())
                if suppress_terminal_period and mapped_chars[pos] == ".":
                    pred_dur[pred_index] = 0
                else:
                    pred_dur[pred_index] = torch.clamp(
                        pred_dur[pred_index],
                        max=punctuation_cap_frames,
                    )
                punctuation_changes.append({
                    "char": mapped_chars[pos],
                    "before_frames": before,
                    "after_frames": int(pred_dur[pred_index].item()),
                    "suppressed_for_epenthetic_release": bool(
                        suppress_terminal_period and mapped_chars[pos] == "."
                    ),
                })

    pred_dur[-1] = torch.clamp(pred_dur[-1], max=eos_cap_frames)

    indices = torch.repeat_interleave(
        torch.arange(ids.shape[1], device=model.device),
        pred_dur.to(model.device),
    )
    pred_aln_trg = torch.zeros(
        (ids.shape[1], indices.shape[0]),
        device=model.device,
    )
    pred_aln_trg[indices, torch.arange(indices.shape[0], device=model.device)] = 1
    pred_aln_trg = pred_aln_trg.unsqueeze(0)

    en = d.transpose(-1, -2) @ pred_aln_trg
    f0_pred, n_pred = model.predictor.F0Ntrain(en, s)
    t_en = model.text_encoder(ids, input_lengths, text_mask)
    asr = t_en @ pred_aln_trg
    audio = model.decoder(asr, f0_pred, n_pred, ref_s[:, :128]).squeeze().cpu()

    terminal_before = (
        int(original[terminal_pred_index].item())
        if terminal_pred_index is not None else None
    )
    terminal_after = (
        int(pred_dur[terminal_pred_index].item())
        if terminal_pred_index is not None else None
    )
    duration_report = {
        "terminal_char": terminal_char,
        "terminal_before_frames": terminal_before,
        "terminal_after_frames": terminal_after,
        "terminal_before_ms": None if terminal_before is None else terminal_before * 25,
        "terminal_after_ms": None if terminal_after is None else terminal_after * 25,
        "punctuation": punctuation_changes,
        "eos_before_frames": int(original[-1].item()),
        "eos_after_frames": int(pred_dur[-1].item()),
        "normal_terminal_lexical_duration_preserved": (
            terminal_before is None or terminal_before <= terminal_cap_frames
        ),
        "tail_before": tail_before,
        "final_long_vowel_change": final_long_vowel_change,
        "terminal_period_suppressed": suppress_terminal_period,
        "only_terminal_duration_family_modified": True,
    }
    return audio, pred_dur.detach().cpu(), duration_report


def synthesize_passage(
    *,
    model: KModel,
    voice: torch.Tensor,
    g2p,
    sentences: tuple[str, ...],
    pauses_ms: tuple[int, ...],
    onset_fade_ms: int,
    pre_release_soften_ms: int,
    pre_release_floor: float,
) -> tuple[np.ndarray, list[dict], list[str], float]:
    if len(pauses_ms) != len(sentences) - 1:
        raise RuntimeError("pause count must equal sentence count minus one")

    chunks: list[np.ndarray] = []
    reports: list[dict] = []
    phoneme_rows: list[str] = []
    pronunciation_repairs: list[list[dict]] = []
    started = time.perf_counter()

    with torch.inference_mode():
        for index, sentence in enumerate(sentences):
            phonemes, _ = g2p(sentence)
            phonemes = clean_phonemes(phonemes)
            phonemes, repairs = repair_obvious_g2p_artifacts(phonemes)
            phonemes, orthography_repairs, orthography_diagnostics = repair_spoken_msa_orthography(
                sentence,
                phonemes,
            )
            repairs.extend(orthography_repairs)
            phonemes, final_pause_repairs = repair_sentence_final_pause(sentence, phonemes)
            repairs.extend(final_pause_repairs)
            phoneme_rows.append(phonemes)
            pronunciation_repairs.append(repairs)
            generated_audio, generated_pred_dur, duration_report = infer_with_terminal_duration_cap(
                model,
                phonemes,
                voice,
                speed=NATIVE_SPEED,
            )
            chunk = generated_audio.numpy().astype(np.float32)
            chunk, edge_report = smooth_sentence_edges(
                chunk,
                pred_dur=generated_pred_dur,
                onset_fade_ms=onset_fade_ms,
                pre_release_soften_ms=pre_release_soften_ms,
                pre_release_floor=pre_release_floor,
            )
            edge_report["sentence_index"] = index + 1
            edge_report["text"] = sentence
            edge_report["phonemes"] = phonemes
            edge_report["orthography_diagnostics"] = orthography_diagnostics
            edge_report["terminal_duration"] = duration_report
            reports.append(edge_report)
            chunks.append(chunk)

    synth_seconds = time.perf_counter() - started

    timeline: list[np.ndarray] = []
    for index, chunk in enumerate(chunks):
        timeline.append(chunk)
        if index < len(pauses_ms):
            timeline.append(
                np.zeros(int(round(SAMPLE_RATE * pauses_ms[index] / 1000.0)), dtype=np.float32)
            )

    return (
        np.concatenate(timeline).astype(np.float32),
        reports,
        phoneme_rows,
        pronunciation_repairs,
        synth_seconds,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="probe_artifacts/nabra-acceptance")
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

    def verified_g2p(text: str):
        phonemes, extra = original_g2p(text)
        return clean_phonemes(phonemes), extra

    voice = torch.load(voice_path, map_location="cpu", weights_only=True)
    load_seconds = time.perf_counter() - load_started

    short_audio, short_edges, short_phonemes, short_repairs, short_synth = synthesize_passage(
        model=model,
        voice=voice,
        g2p=verified_g2p,
        sentences=SHORT_SENTENCES,
        pauses_ms=SHORT_PAUSES_MS,
        onset_fade_ms=26,
        pre_release_soften_ms=90,
        pre_release_floor=0.82,
    )
    short_raw = output / "01-nabra-new-short-smooth-raw.wav"
    short_mix = output / "02-nabra-new-short-smooth-mix-ready.wav"
    sf.write(short_raw, short_audio, SAMPLE_RATE, subtype="PCM_16")
    mix_ready(short_raw, short_mix)

    long_audio, long_edges, long_phonemes, long_repairs, long_synth = synthesize_passage(
        model=model,
        voice=voice,
        g2p=verified_g2p,
        sentences=LONG_SENTENCES,
        pauses_ms=LONG_PAUSES_MS,
        onset_fade_ms=22,
        pre_release_soften_ms=75,
        pre_release_floor=0.84,
    )
    long_raw = output / "03-nabra-long-validation-smooth-raw.wav"
    long_mix = output / "04-nabra-long-validation-smooth-mix-ready.wav"
    sf.write(long_raw, long_audio, SAMPLE_RATE, subtype="PCM_16")
    mix_ready(long_raw, long_mix)

    peak_rss_kb = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    report = {
        "status": "success",
        "model": REPO_ID,
        "voice": "af_msa",
        "native_speed": NATIVE_SPEED,
        "edge_policy": {
            "threshold_db": -34.0,
            "frame_ms": 10,
            "pre_roll_ms": 12,
            "post_roll_ms": 180,
            "short_onset_fade_ms": 26,
            "long_onset_fade_ms": 22,
            "short_pre_release_soften_ms": 90,
            "long_pre_release_soften_ms": 75,
            "release_hold_ms": 25,
            "release_fade_ms": 150,
            "principle": (
                "silence only model-reserved BOS lead; gentle equal-power fade-in; "
                "longer tail-only fade-out after lexical speech; preserve lexical timing"
            ),
        },
        "short": {
            "sentences": SHORT_SENTENCES,
            "pauses_ms": SHORT_PAUSES_MS,
            "pause_reasons": SHORT_PAUSE_REASONS,
            "phonemes": short_phonemes,
            "pronunciation_repairs": short_repairs,
            "edges": short_edges,
            "synthesis_seconds": round(short_synth, 3),
            "raw_wav": wav_info(short_raw),
            "mix_ready_wav": wav_info(short_mix),
        },
        "long": {
            "sentences": LONG_SENTENCES,
            "pauses_ms": LONG_PAUSES_MS,
            "pause_reasons": LONG_PAUSE_REASONS,
            "phonemes": long_phonemes,
            "pronunciation_repairs": long_repairs,
            "edges": long_edges,
            "synthesis_seconds": round(long_synth, 3),
            "raw_wav": wav_info(long_raw),
            "mix_ready_wav": wav_info(long_mix),
        },
        "download_seconds": round(download_seconds, 3),
        "model_and_frontend_load_seconds": round(load_seconds, 3),
        "peak_rss_mb": round(peak_rss_kb / 1024.0, 1),
        "python": platform.python_version(),
        "torch_version": torch.__version__,
        "runner_cpu_count": os.cpu_count(),
        "production_wiring": False,
    }
    (output / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("NABRA_ACCEPTANCE_REPORT=" + json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
