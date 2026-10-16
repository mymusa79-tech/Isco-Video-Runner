"""Versioned, offline-only import of an approved production.json + image assets.

The archive is inspected before anything is extracted. Telegram stores the manifest
and archive digest in the existing request; production downloads the same file and
checks that digest again. No URL, code, stock search, model or voice override is input.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import stat
import zipfile
from pathlib import Path
from typing import Any, Mapping

from .owner_pack import LONG_TYPES, SHORT_TYPES, OwnerPackError

MAX_ZIP_BYTES = 18 * 1024 * 1024  # below Telegram's standard getFile limit
MAX_EXPANDED_BYTES = 96 * 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_JSON_BYTES = 300 * 1024
MAX_FILES = 65
MAX_PIXELS = 25_000_000
_ASSET = re.compile(r"assets/[A-Za-z0-9_-]+\.(?:png|jpg|jpeg|webp)\Z", re.I)
_ID = re.compile(r"[a-z][a-z0-9_]{0,30}\Z")


def _error(message: str) -> None:
    raise OwnerPackError(message)


def _object(value: Any, required: set[str], optional: set[str] = frozenset(), *, field: str) -> dict:
    if not isinstance(value, dict) or required - value.keys() or value.keys() - required - optional:
        _error(f"حقول {field} غير صالحة؛ المطلوبة: {', '.join(sorted(required))}.")
    return value


def _text(value: Any, field: str, maximum: int = 1000, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()) or "\x00" in value:
        _error(f"النص في {field} فارغ أو غير صالح أو طويل جدًا.")
    return value


def _asset(value: Any) -> str:
    if not isinstance(value, str) or not _ASSET.fullmatch(value):
        _error("مسار الصورة يجب أن يكون مثل assets/scene_01.png بأحرف إنجليزية وأرقام.")
    return value


def section_text(section: Mapping[str, Any]) -> str:
    if "turns" in section:
        return " ".join(f"{t['speaker']}: {t['text']}" for t in section["turns"])
    return str(section["text"])


def spoken_text(text: str) -> str:
    return " ".join(re.sub(r"(?<!\S)[AB]:\s*", "", text).split())


def validate_manifest(value: Any, available: set[str] | None = None) -> dict[str, Any]:
    doc = _object(value, {"schema_version", "format", "template", "title", "approval", "script", "performance",
                          "scenes", "editing", "captions", "cover", "publishing"}, {"derivative", "roles"}, field="production.json")
    if type(doc["schema_version"]) is not int or doc["schema_version"] != 1:
        _error("schema_version يجب أن يساوي 1.")
    fmt = doc["format"]
    allowed = {"short": (*SHORT_TYPES, "ala_mahl"), "film": LONG_TYPES, "podcast": ("dialogue_qa",)}
    if not isinstance(fmt, str) or fmt not in allowed or doc["template"] not in allowed[fmt]:
        _error("النوع أو القالب غير مدعوم.")
    _text(doc["title"], "title", 120)
    approval = _object(doc["approval"], {"status", "reviewer"}, field="approval")
    if approval != {"status": "approved", "reviewer": "Claude"}:
        _error("الحزمة يجب أن تكون معتمدة ومعلّمة بمراجعة Claude قبل إرسالها.")
    script = _object(doc["script"], {"sections"}, field="script")
    sections = script["sections"]
    if not isinstance(sections, list) or not (len(sections) == (3 if fmt == "short" else 5) if fmt != "podcast" else 2 <= len(sections) <= 5):
        _error("عدد الأقسام: 3 للشورت، 5 للفيلم، و2–5 للبودكاست.")
    texts: dict[str, str] = {}
    for index, section in enumerate(sections, 1):
        _object(section, {"id", "turns" if fmt == "podcast" else "text"}, field=f"section {index}")
        if section["id"] != f"s{index}":
            _error("معرّفات الأقسام يجب أن تكون s1 ثم s2 وهكذا.")
        if fmt == "podcast":
            turns = section["turns"]
            if not isinstance(turns, list) or not 2 <= len(turns) <= 200:
                _error("قسم البودكاست يحتاج أدوار A وB مرتّبة.")
            for turn in turns:
                _object(turn, {"speaker", "text"}, field="turn")
                if turn["speaker"] not in ("A", "B"):
                    _error("أدوار البودكاست محصورة في A وB.")
                _text(turn["text"], "turn.text", 20000)
                if re.search(r"(?<!\S)[AB]:", turn["text"]):
                    _error("ضع المتحدث في speaker فقط، دون تسميات متداخلة داخل النص.")
        else:
            _text(section["text"], "section.text", 30000)
            if len(section["text"].strip()) < 20 or re.search(r"(?<!\S)[AB]:", section["text"]):
                _error("قسم السرد يحتاج 20 حرفًا على الأقل، دون أدوار A/B.")
        texts[section["id"]] = section_text(section)
    from .identity_sequence import PRAYER_SENTENCE, SHORT_CHANNEL_DEFINITION, SHORT_CHANNEL_DEFINITION_LEGACY, LONG_CHANNEL_DEFINITION
    whole = " ".join(texts.values())
    if any(phrase in whole for phrase in (PRAYER_SENTENCE, SHORT_CHANNEL_DEFINITION, SHORT_CHANNEL_DEFINITION_LEGACY, LONG_CHANNEL_DEFINITION)):
        _error("احذف الهوية الثابتة والصلاة من النص؛ يضيفهما المسار الحالي مرة واحدة.")
    if not re.search(r"[.!؟!]", texts["s1"]):
        _error("ضع علامة ترقيم بعد جملة الهوك الأولى.")
    if fmt == "podcast":
        if doc.get("roles") != {"A": "listener", "B": "host"}:
            _error('roles يجب أن تكون {"A":"listener","B":"host"}؛ الأصوات الحالية ثابتة.')
        from .pipeline import _validate_podcast_listener_proxy_script
        try:
            _validate_podcast_listener_proxy_script({"sections": [{"narration": t} for t in texts.values()]})
        except RuntimeError as exc:
            _error("ترتيب حوار البودكاست غير صالح: " + str(exc)[:180])
    elif "roles" in doc:
        _error("roles متاحة للبودكاست فقط.")
    performance = _object(doc["performance"], {"direction"}, {"A", "B"}, field="performance")
    for key, direction in performance.items():
        _text(direction, f"performance.{key}", 800, empty=key == "direction")
        if key in {"A", "B"} and fmt != "podcast":
            _error("توجيهات A/B للبودكاست فقط.")
    editing = _object(doc["editing"], {"music"}, field="editing")
    if editing["music"] != "none":
        _error('الإصدار الأول يدعم editing.music="none" فقط، دون موسيقى مضافة.')
    if doc["captions"] != {"font": "Cairo Bold", "max_lines": 2, "enabled": True}:
        _error("الكابشن يجب أن يكون مفعّلًا بخط Cairo Bold وسطرين كحد أقصى.")
    scenes = doc["scenes"]
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= 30:
        _error("الحزمة تحتاج 1–30 مشهدًا يغطي كل الأقسام.")
    if doc["template"] == "ala_mahl" and not 3 <= len(scenes) <= 6:
        _error("على مهل يحتاج 3–6 صور قصصية مرتّبة.")
    seen: set[str] = set()
    assets: set[str] = set()
    prior = (-1, -1)
    covered = set()
    for scene in scenes:
        _object(scene, {"id", "section_id", "asset", "start_text", "motion", "transition"}, field="scene")
        sid = scene["section_id"]
        identifier = scene["id"]
        if not isinstance(identifier, str) or not _ID.fullmatch(identifier) or identifier in seen or sid not in texts:
            _error("معرّف مشهد مكرر أو غير صالح، أو ربطه بقسم غير موجود.")
        seen.add(identifier)
        assets.add(_asset(scene["asset"]))
        anchor = _text(scene["start_text"], "scene.start_text", 300)
        haystack = spoken_text(texts[sid])
        if haystack.count(anchor) != 1:
            _error(f"بداية المشهد {identifier} يجب أن تطابق نصًا واحدًا حرفيًا داخل {sid}.")
        position = haystack.index(anchor)
        order = (int(sid[1:]), position)
        if order <= prior or (sid not in covered and position != 0):
            _error("رتّب المشاهد بحسب النص؛ أول مشهد في كل قسم يبدأ من أول كلماته.")
        prior = order
        covered.add(sid)
        if scene["motion"] not in ("slow_pan", "slow_zoom", "hold") or scene["transition"] not in ("cut", "dissolve"):
            _error("المونتاج يدعم slow_pan / slow_zoom / hold وانتقال cut / dissolve فقط.")
    if covered != set(texts):
        _error("يجب أن تغطي الصور جميع أقسام النص.")
    if doc["template"] == "ala_mahl" and len(assets) != len(scenes):
        _error("على مهل يحتاج صورة أصلية مختلفة لكل مشهد؛ لا تكرر الصورة.")
    cover = _object(doc["cover"], {"asset", "text"}, field="cover")
    _text(cover["text"], "cover.text", 100, empty=True)
    if len(cover["text"].split()) > 6:
        _error("نص الغلاف لا يتجاوز 6 كلمات.")
    if cover["asset"] is not None:
        assets.add(_asset(cover["asset"]))
    publishing = _object(doc["publishing"], {"title", "description", "hashtags"}, field="publishing")
    _text(publishing["title"], "publishing.title", 120)
    _text(publishing["description"], "publishing.description", 5000, empty=True)
    if not isinstance(publishing["hashtags"], list) or len(publishing["hashtags"]) > 15:
        _error("hashtags قائمة من 15 وسمًا كحد أقصى.")
    for tag in publishing["hashtags"]:
        if not isinstance(tag, str) or not re.fullmatch(r"#[\w\u0600-\u06ff]{1,80}", tag):
            _error("الهاشتاق يجب أن يبدأ بـ # دون فراغات.")
    derivative = doc.get("derivative")
    if derivative is not None:
        _object(derivative, {"start_text", "end_text"}, field="derivative")
        if fmt == "short":
            _error("المشتق متاح للفيلم والبودكاست فقط.")
        for key in ("start_text", "end_text"):
            _text(derivative[key], f"derivative.{key}", 1500)
        # Invalid/missing anchors are intentionally NOT a parent-production gate.
        # The optional extractor records the precise skip reason after parent success.
    if available is not None and assets - available:
        _error("صور أساسية مفقودة: " + "، ".join(sorted(assets - available)))
    return doc


def is_zip_document(message: Mapping[str, Any]) -> bool:
    doc = message.get("document") or {}
    return str(doc.get("file_name") or "").lower().endswith(".zip") or str(doc.get("mime_type") or "").lower() in {"application/zip", "application/x-zip-compressed"}


def _unique_object(pairs: list) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            _error("JSON يحتوي حقلًا مكررًا: " + key)
        result[key] = value
    return result


def inspect_archive(data: bytes) -> dict[str, Any]:
    if not data or len(data) > MAX_ZIP_BYTES:
        _error("حجم ZIP يجب ألا يتجاوز 18MB.")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES or sum(i.file_size for i in entries) > MAX_EXPANDED_BYTES:
                _error("ZIP ضخم بعد فك الضغط أو يحتوي ملفات كثيرة.")
            files: dict[str, bytes] = {}
            names: set[str] = set()
            for item in entries:
                name = item.filename
                mode = item.external_attr >> 16
                if name.casefold() in names or item.flag_bits & 1 or stat.S_ISLNK(mode):
                    _error("ZIP يحتوي مسارًا مكررًا أو ملفًا مشفّرًا أو رابطًا رمزيًا.")
                names.add(name.casefold())
                if name == "assets/" and item.is_dir():
                    continue
                if name != "production.json" and not _ASSET.fullmatch(name):
                    _error("ملف أو مسار غير مسموح داخل ZIP: " + name[:100])
                if stat.S_IFMT(mode) not in {0, stat.S_IFREG} or item.is_dir():
                    _error("ZIP يسمح بالملفات العادية فقط.")
                limit = MAX_JSON_BYTES if name == "production.json" else MAX_IMAGE_BYTES
                if item.file_size > limit or item.file_size > max(1, item.compress_size) * 150:
                    _error("ملف ضخم أو نسبة ضغط غير آمنة داخل ZIP.")
                with archive.open(item) as stream:
                    content = stream.read(limit + 1)
                if len(content) != item.file_size or len(content) > limit:
                    _error("حجم ملف ZIP غير مطابق.")
                files[name] = content
            if "production.json" not in files:
                _error("production.json غير موجود في جذر ZIP.")
            doc = json.loads(files["production.json"].decode("utf-8-sig"), object_pairs_hook=_unique_object,
                             parse_constant=lambda _v: _error("قيم JSON غير صالحة."))
            validate_manifest(doc, set(files) - {"production.json"})
            from PIL import Image
            for name, content in files.items():
                if name == "production.json":
                    continue
                with Image.open(io.BytesIO(content)) as image:
                    if image.format not in {"PNG", "JPEG", "WEBP"} or image.width * image.height > MAX_PIXELS or getattr(image, "n_frames", 1) != 1:
                        _error("صورة غير مدعومة أو ضخمة أو متحركة: " + name)
                    if doc["template"] == "ala_mahl" and name in {s["asset"] for s in doc["scenes"]} and abs(image.width / image.height - 9 / 16) > 0.035:
                        _error("صور على مهل الأساسية يجب أن تكون عمودية 9:16: " + name)
                    image.verify()
                # JPEG verify() checks structure only; decode as well, before a
                # confirmed run can spend voice quota on an unreadable picture.
                with Image.open(io.BytesIO(content)) as image:
                    image.load()
            return {"manifest": doc, "zip_sha256": hashlib.sha256(data).hexdigest(),
                    "asset_sha256": {n: hashlib.sha256(b).hexdigest() for n, b in files.items() if n != "production.json"},
                    "files": files}
    except OwnerPackError:
        raise
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, EOFError, zipfile.BadZipFile, NotImplementedError) as exc:
        raise OwnerPackError("ZIP أو JSON أو إحدى الصور تالف أو غير قابل للقراءة.") from exc


def import_message(message: Mapping[str, Any], download) -> dict[str, Any]:
    document = message["document"]
    if int(document.get("file_size") or 0) > MAX_ZIP_BYTES:
        _error("حجم ZIP يجب ألا يتجاوز 18MB.")
    inspected = inspect_archive(download(str(document["file_id"]), max_bytes=MAX_ZIP_BYTES))
    doc = inspected["manifest"]
    return {"schema_version": 1, "format": doc["format"], "title": doc["title"], "production": doc,
            "zip_sha256": inspected["zip_sha256"], "asset_sha256": inspected["asset_sha256"]}


def extract_archive(data: bytes, directory: Path, *, expected_sha256: str) -> dict:
    if hashlib.sha256(data).hexdigest() != expected_sha256:
        _error("تغير ZIP بعد اعتماد الحزمة؛ لن يبدأ الإنتاج.")
    inspected = inspect_archive(data)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    if any(directory.iterdir()):
        _error("استخراج ZIP يتطلب مجلدًا فارغًا.")
    for name, content in inspected["files"].items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return inspected["manifest"]
