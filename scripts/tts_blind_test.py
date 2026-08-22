from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


HARNESS_VERSION = "tts-blind-v1"
DEFAULT_GEMINI_MODEL = "gemini-3.1-flash-tts-preview"
DEFAULT_GEMINI_VOICE = "Charon"
ENGINES = ("gemini", "voxcpm2", "chatterbox_multilingual_v3")
BLIND_SEED = "isco-tts-blind-v1-fixed"

RUBRIC = {
    "naturalness": 20,
    "arabic_pronunciation": 15,
    "prosody_and_pauses": 15,
    "emotional_fit": 12,
    "clarity_intelligibility": 12,
    "warmth_presence": 10,
    "consistency": 8,
    "long_form_fatigue": 8,
}


class AdapterUnavailable(RuntimeError):
    pass


class GenerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class CorpusItem:
    sample_id: str
    direction: str
    text: str


def _read_corpus(path: Path) -> list[CorpusItem]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("TTS corpus must be schema_version=1 object")
    rows = data.get("samples")
    if not isinstance(rows, list) or len(rows) != 10:
        raise ValueError("TTS blind corpus must contain exactly 10 samples")
    result: list[CorpusItem] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Corpus sample must be an object")
        sample_id = str(row.get("id") or "").strip()
        direction = str(row.get("direction") or "").strip()
        text = str(row.get("text") or "").strip()
        if not sample_id or sample_id in seen or not direction or not text:
            raise ValueError("Corpus sample id/direction/text must be non-empty and ids unique")
        seen.add(sample_id)
        result.append(CorpusItem(sample_id, direction, text))
    return result


def _gemini_prompt(item: CorpusItem) -> str:
    return (
        "اقرأ النص التالي فقط دون إضافة أو حذف. التزم بالعربية الفصحى الطبيعية. "
        "تجنب نبرة المذيع والإعلان والمبالغة المسرحية. "
        f"توجيه الأداء: {item.direction}\n\nالنص: {item.text}"
    )


def _generate_gemini(item: CorpusItem, output: Path) -> None:
    api_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if not api_key:
        key_file = (os.environ.get("GEMINI_API_KEY_FILE") or "").strip()
        if key_file:
            try:
                api_key = Path(key_file).read_text(encoding="utf-8").strip()
            except OSError:
                api_key = ""
    if not api_key:
        raise AdapterUnavailable("gemini_api_key_missing")
    try:
        from google import genai
    except ImportError as exc:
        raise AdapterUnavailable("google_genai_not_installed") from exc

    model = (os.environ.get("ISCO_TTS_BLIND_GEMINI_MODEL") or DEFAULT_GEMINI_MODEL).strip()
    voice = (os.environ.get("ISCO_TTS_BLIND_GEMINI_VOICE") or DEFAULT_GEMINI_VOICE).strip()
    client = genai.Client(api_key=api_key)
    interaction = client.interactions.create(
        model=model,
        input=_gemini_prompt(item),
        response_format={"type": "audio"},
        generation_config={"speech_config": [{"voice": voice}]},
    )
    if not interaction.output_audio or not interaction.output_audio.data:
        raise GenerationError("gemini_returned_no_audio")
    pcm = base64.b64decode(interaction.output_audio.data)
    output.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(output), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(24000)
        wav.writeframes(pcm)


def _command_adapter(engine: str, item: CorpusItem, output: Path) -> None:
    env_name = {
        "voxcpm2": "ISCO_VOXCPM2_TTS_ARGV_JSON",
        "chatterbox_multilingual_v3": "ISCO_CHATTERBOX_V3_TTS_ARGV_JSON",
    }[engine]
    raw = (os.environ.get(env_name) or "").strip()
    if not raw:
        raise AdapterUnavailable(f"{env_name.lower()}_missing")
    try:
        template = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AdapterUnavailable(f"{env_name.lower()}_invalid_json") from exc
    if not isinstance(template, list) or not template or any(not isinstance(x, str) for x in template):
        raise AdapterUnavailable(f"{env_name.lower()}_must_be_json_string_array")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f"isco-{engine}-") as temp_dir:
        text_file = Path(temp_dir) / "text.txt"
        direction_file = Path(temp_dir) / "direction.txt"
        text_file.write_text(item.text, encoding="utf-8")
        direction_file.write_text(item.direction, encoding="utf-8")
        replacements = {
            "{text_file}": str(text_file),
            "{direction_file}": str(direction_file),
            "{output_wav}": str(output),
            "{sample_id}": item.sample_id,
        }
        argv: list[str] = []
        for token in template:
            value = token
            for marker, replacement in replacements.items():
                value = value.replace(marker, replacement)
            argv.append(value)
        subprocess.run(argv, check=True, shell=False)
    if not output.is_file() or output.stat().st_size < 512:
        raise GenerationError(f"{engine}_output_missing_or_empty")


def _adapter(engine: str) -> Callable[[CorpusItem, Path], None]:
    if engine == "gemini":
        return _generate_gemini
    if engine in {"voxcpm2", "chatterbox_multilingual_v3"}:
        return lambda item, output: _command_adapter(engine, item, output)
    raise ValueError(f"unsupported engine: {engine}")


def _blind_order(sample_id: str) -> list[str]:
    digest = hashlib.sha256(f"{BLIND_SEED}:{sample_id}".encode()).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))
    values = list(ENGINES)
    rng.shuffle(values)
    return values


def _rubric_template(corpus: list[CorpusItem], mapping: dict[str, dict[str, str]]) -> dict:
    samples = []
    for item in corpus:
        labels = sorted(mapping[item.sample_id])
        samples.append(
            {
                "sample_id": item.sample_id,
                "scores": {
                    label: {dimension: None for dimension in RUBRIC}
                    for label in labels
                },
                "notes": {label: "" for label in labels},
            }
        )
    return {
        "schema_version": 1,
        "harness_version": HARNESS_VERSION,
        "scale": "1-10, higher is better",
        "rubric_weights_percent": RUBRIC,
        "samples": samples,
    }


def generate(corpus_path: Path, output_dir: Path) -> dict:
    corpus = _read_corpus(corpus_path)
    raw_dir = output_dir / "raw"
    blind_dir = output_dir / "blind"
    raw_dir.mkdir(parents=True, exist_ok=True)
    blind_dir.mkdir(parents=True, exist_ok=True)

    status: dict[str, dict[str, dict[str, str]]] = {}
    mapping: dict[str, dict[str, str]] = {}
    for item in corpus:
        order = _blind_order(item.sample_id)
        mapping[item.sample_id] = {}
        status[item.sample_id] = {}
        for label, engine in zip(("A", "B", "C"), order):
            mapping[item.sample_id][label] = engine
            raw_path = raw_dir / engine / f"{item.sample_id}.wav"
            try:
                _adapter(engine)(item, raw_path)
                blind_path = blind_dir / f"{item.sample_id}-{label}.wav"
                shutil.copyfile(raw_path, blind_path)
                status[item.sample_id][engine] = {"status": "generated", "file": str(raw_path)}
            except AdapterUnavailable as exc:
                status[item.sample_id][engine] = {"status": "unavailable", "reason": str(exc)}
            except Exception as exc:
                status[item.sample_id][engine] = {
                    "status": "error",
                    "reason": f"{type(exc).__name__}: {str(exc)[:240]}",
                }

    output_dir.mkdir(parents=True, exist_ok=True)
    mapping_doc = {
        "schema_version": 1,
        "harness_version": HARNESS_VERSION,
        "production_decision_frozen": False,
        "mapping": mapping,
    }
    (output_dir / "blind-mapping.json").write_text(
        json.dumps(mapping_doc, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "generation-status.json").write_text(
        json.dumps({"schema_version": 1, "status": status}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (output_dir / "scores-template.json").write_text(
        json.dumps(_rubric_template(corpus, mapping), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return {"mapping": mapping, "status": status}


def _score_value(value: object, *, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where} must be numeric")
    score = float(value)
    if not 1.0 <= score <= 10.0:
        raise ValueError(f"{where} must be between 1 and 10")
    return score


def score(scores_path: Path, mapping_path: Path, output_path: Path) -> dict:
    scores_doc = json.loads(scores_path.read_text(encoding="utf-8"))
    mapping_doc = json.loads(mapping_path.read_text(encoding="utf-8"))
    mapping = mapping_doc.get("mapping") if isinstance(mapping_doc, dict) else None
    rows = scores_doc.get("samples") if isinstance(scores_doc, dict) else None
    if not isinstance(mapping, dict) or not isinstance(rows, list):
        raise ValueError("invalid blind-test score or mapping document")

    engine_totals = {engine: 0.0 for engine in ENGINES}
    engine_counts = {engine: 0 for engine in ENGINES}
    per_sample: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("score sample must be object")
        sample_id = str(row.get("sample_id") or "")
        labels = mapping.get(sample_id)
        row_scores = row.get("scores")
        if not isinstance(labels, dict) or not isinstance(row_scores, dict):
            raise ValueError(f"missing mapping/scores for {sample_id}")
        sample_result = {"sample_id": sample_id, "engines": {}}
        for label, engine in labels.items():
            dimensions = row_scores.get(label)
            if not isinstance(dimensions, dict):
                raise ValueError(f"missing scores for {sample_id}/{label}")
            weighted = 0.0
            for dimension, weight in RUBRIC.items():
                value = _score_value(dimensions.get(dimension), where=f"{sample_id}/{label}/{dimension}")
                weighted += value * weight / 100.0
            weighted = round(weighted, 4)
            sample_result["engines"][engine] = weighted
            engine_totals[engine] += weighted
            engine_counts[engine] += 1
        per_sample.append(sample_result)

    averages = {
        engine: round(engine_totals[engine] / engine_counts[engine], 4)
        for engine in ENGINES
        if engine_counts[engine]
    }
    ranking = sorted(averages, key=lambda engine: (-averages[engine], engine))
    result = {
        "schema_version": 1,
        "harness_version": HARNESS_VERSION,
        "production_decision_frozen": False,
        "rubric_weights_percent": RUBRIC,
        "engine_weighted_average_1_to_10": averages,
        "raw_ranking": ranking,
        "samples": per_sample,
        "note": "Raw blind-test result only; this harness does not change the production voice roster.",
    }
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Three-engine Arabic TTS blind-test harness")
    sub = parser.add_subparsers(dest="command", required=True)

    generate_parser = sub.add_parser("generate")
    generate_parser.add_argument("--corpus", type=Path, default=Path("scripts/tts_blind_corpus_ar.json"))
    generate_parser.add_argument("--output", type=Path, required=True)

    score_parser = sub.add_parser("score")
    score_parser.add_argument("--scores", type=Path, required=True)
    score_parser.add_argument("--mapping", type=Path, required=True)
    score_parser.add_argument("--output", type=Path, required=True)

    args = parser.parse_args()
    if args.command == "generate":
        result = generate(args.corpus, args.output)
        print(json.dumps(result["status"], ensure_ascii=False, indent=2))
    else:
        result = score(args.scores, args.mapping, args.output)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
