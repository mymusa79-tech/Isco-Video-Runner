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
from .pinned_stock import parse_stock_url

MAX_ZIP_BYTES = 18 * 1024 * 1024  # below Telegram's standard getFile limit
MAX_EXPANDED_BYTES = 96 * 1024 * 1024
MAX_IMAGE_BYTES = 12 * 1024 * 1024
MAX_JSON_BYTES = 300 * 1024
MAX_FILES = 150  # up to 60 scenes + covers + production.json; the real ceiling is MAX_ZIP_BYTES
MAX_PIXELS = 25_000_000
_ASSET = re.compile(r"assets/[A-Za-z0-9_-]+\.(?:png|jpg|jpeg|webp)\Z", re.I)
_ID = re.compile(r"[a-z][a-z0-9_]{0,30}\Z")


def _error(message: str) -> None:
    raise OwnerPackError(message)


def _object(value: Any, required: set[str], optional: set[str] = frozenset(), *, field: str) -> dict:
    if not isinstance(value, dict) or required - value.keys() or value.keys() - required - optional:
        _error(f"حقول {field} غير صالحة؛ المطلوبة: {', '.join(sorted(required))}.")
    return value


def _publishing(value: Any, field: str) -> dict:
    """YouTube-ready title / description / hashtags (and optional tags) with the platform's own hard limits."""
    pub = _object(value, {"title", "description", "hashtags"}, {"tags"}, field=field)
    _text(pub["title"], f"{field}.title", 100)  # YouTube rejects titles over 100 characters
    _text(pub["description"], f"{field}.description", 5000, empty=True)
    if len(pub["description"].encode("utf-8")) > 5000:  # YouTube counts bytes: Arabic letters take two
        _error(f"وصف {field} يتجاوز 5000 بايت (حد يوتيوب؛ الحرف العربي بايتان).")
    if not isinstance(pub["hashtags"], list) or len(pub["hashtags"]) > 15:
        _error("hashtags قائمة من 15 وسمًا كحد أقصى.")
    for tag in pub["hashtags"]:
        if not isinstance(tag, str) or not re.fullmatch(r"#[\w\u0600-\u06ff]{1,80}", tag):
            _error("الهاشتاق يجب أن يبدأ بـ # دون فراغات.")
    if "tags" in pub:
        tags = pub["tags"]
        if (not isinstance(tags, list) or len(tags) > 15
                or any(not isinstance(t, str) or not t.strip() or len(t) > 40 or "," in t or "\x00" in t for t in tags)
                or len(",".join(tags)) > 450):
            _error("tags قائمة من 15 وسمًا نصيًا كحد أقصى (كل منها ≤40 حرفًا بلا فاصلة، والمجموع ≤450).")
    return pub


def _text(value: Any, field: str, maximum: int = 1000, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()) or "\x00" in value:
        _error(f"النص في {field} فارغ أو غير صالح أو طويل جدًا.")
    return value


# Optional real-stock scene (Short, Film and Podcast). The scene's required `asset` stays the
# approved image used whenever the client cannot admit a matching clip, so a stock scene can
# never leave a gap and the archive never carries video. `url` (a Pexels/Pixabay page link the editor
# verified) pins the exact clip or photo; it is only a string and the client still reviews the result.
ALA_MAHL_STOCK_MAX = 2  # ambient/object inserts only; characters stay approved images
# Scene and stock counts are planned per video (no fixed count). These are only technical safety limits.
SCENE_MAX = {"short": 14, "film": 60, "podcast": 40}  # podcast can now change visuals at dialogue anchors
STOCK_MAX_SCENES = {"short": 14, "film": 60, "podcast": 16}  # legacy ZIP cap; new three-link packs cover every scene
THREE_LINK_STOCK_POLICY = "three_urls_per_scene"  # explicit contract for newly authored packages
_STOCK_QUERY = re.compile(r"[A-Za-z][A-Za-z '\-]{2,78}[A-Za-z]")


def _stock(value: Any, fmt: str, template: str) -> dict:
    if fmt not in STOCK_MAX_SCENES:
        _error("لقطات الستوك لنداء شورت والفيلم وعلى مهل والبودكاست فقط.")
    pinned = isinstance(value, dict) and ("url" in value or "urls" in value)
    stock = _object(value, set() if pinned else {"query_en"},
                    {"query_en", "query_alt_en", "must_show", "url", "urls", "mode"}, field="scene.stock")
    if "url" in stock and "urls" in stock:
        _error("اختر stock.url أو stock.urls وليس الاثنين.")
    if pinned:
        from .pinned_stock import parse_stock_url

        urls = stock.get("urls") if "urls" in stock else [stock["url"]]
        if not isinstance(urls, list) or not 1 <= len(urls) <= 3:
            _error("stock.urls يجب أن تحتوي 1–3 روابط مرتبة حسب الأفضلية.")
        try:
            ids = [parse_stock_url(url)[:3:2] for url in urls]
        except ValueError:
            _error("stock.urls: روابط صفحات HTTPS من Pexels أو Pixabay أو Coverr فقط.")
        if len(ids) != len(set(ids)):
            _error("stock.urls: لا تكرر المقطع في روابط المشهد نفسه.")
    # Explicit sequence is opt-in. Legacy stock.urls remain ordered *alternatives*.
    if "mode" in stock:
        if (stock["mode"] != "sequence" or "urls" not in stock
                or len(stock["urls"]) != 3 or fmt not in {"short", "film"}
                or template == "ala_mahl"):
            _error("stock.mode=sequence يحتاج ثلاثة روابط فيديو لشورت نداء أو الفيلم فقط.")
        from .pinned_stock import parse_stock_url
        if any(parse_stock_url(url)[1] != "video" or parse_stock_url(url)[0] not in {"pexels", "pixabay"}
               for url in stock["urls"]):
            _error("stock.mode=sequence: فيديوهات Pexels أو Pixabay فقط؛ الصور تبقى احتياطية.")
    for key in ("query_en", "query_alt_en"):
        if key in stock and (not isinstance(stock[key], str) or not _STOCK_QUERY.fullmatch(stock[key].strip())
                             or not 2 <= len(stock[key].split()) <= 9):
            _error(f"{key} استعلام إنجليزي من 2–9 كلمات بأحرف إنجليزية فقط.")
    if "must_show" in stock:
        _text(stock["must_show"], "scene.stock.must_show", 120)
    return stock


def _board(value: Any, fmt: str) -> dict:
    """Optional explainer board on a Film / Podcast scene (see board_scene.py)."""
    from . import board_scene

    if fmt not in board_scene.BOARD_MAX_SCENES:
        _error("اللوحة التوضيحية للفيلم والبودكاست وشورت النداء فقط.")
    board = _object(value, {"layout", "items"}, {"title", "theme"}, field="scene.board")
    layouts = (board_scene.PODCAST_LAYOUTS if fmt == "podcast" else board_scene.SHORT_LAYOUTS if fmt == "short"
               else board_scene.LAYOUTS)
    if board["layout"] not in layouts:
        _error("layout اللوحة: " + " / ".join(sorted(layouts)) + ".")
    if board.get("theme", "paper") not in board_scene.THEMES:
        _error("theme اللوحة: paper أو slate.")
    if "title" in board:
        _text(board["title"], "board.title", board_scene.MAX_TITLE_CHARS, empty=True)
    low, high = layouts[board["layout"]]
    items = board["items"]
    if not isinstance(items, list) or not low <= len(items) <= high:
        _error(f"عناصر لوحة {board['layout']}: من {low} إلى {high}.")
    for item in items:
        _object(item, {"label", "at_text"}, {"asset"}, field="board.item")
        _text(item["label"], "board.item.label", board_scene.MAX_LABEL_CHARS)
        _text(item["at_text"], "board.item.at_text", 200)
        if item.get("asset") is not None:
            _asset(item["asset"])
    return board


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
                          "scenes", "editing", "captions", "cover", "publishing"}, {"derivative", "roles", "stock_policy"}, field="production.json")
    if type(doc["schema_version"]) is not int or doc["schema_version"] != 1:
        _error("schema_version يجب أن يساوي 1.")
    fmt = doc["format"]
    if "stock_policy" in doc and doc["stock_policy"] != THREE_LINK_STOCK_POLICY:
        _error("stock_policy: استخدم three_urls_per_scene أو احذف الحقل للحزم القديمة.")
    three_per_scene = doc.get("stock_policy") == THREE_LINK_STOCK_POLICY
    allowed = {"short": (*SHORT_TYPES, "ala_mahl"), "film": LONG_TYPES, "podcast": ("dialogue_qa", "ala_mahl_dialogue")}
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
    editing = _object(doc["editing"], {"music"}, {"track_id", "asset", "delay_seconds"}, field="editing")
    music_asset = None
    if editing["music"] == "none":
        if len(editing) != 1:
            _error("editing.music=none: لا تضف بيانات موسيقى.")
    elif editing["music"] == "youtube_audio_library":
        from .owner_youtube_music import ASSET, valid_track
        if set(editing) - {"delay_seconds"} != {"music", "track_id", "asset"}:
            _error("موسيقى يوتيوب: يلزم track_id وasset فقط مع delay_seconds اختياري.")
        if editing["asset"] != ASSET or not valid_track(editing["track_id"], fmt, doc["template"]):
            _error("المقطوعة غير معتمدة لهذا النوع أو مسار الملف الموسيقي غير صالح.")
        delay = editing.get("delay_seconds", 3)
        if type(delay) not in (int, float) or not 1 <= delay <= 12:
            _error("تبدأ الموسيقى بعد 1–12 ثانية من بداية الموضوع.")
        music_asset = ASSET
    else:
        _error("editing.music: استخدم none أو youtube_audio_library فقط.")
    if doc["captions"] != {"font": "Cairo Bold", "max_lines": 2, "enabled": True}:
        _error("الكابشن يجب أن يكون مفعّلًا بخط Cairo Bold وسطرين كحد أقصى.")
    scenes = doc["scenes"]
    scene_cap = SCENE_MAX.get(fmt, 30)
    if not isinstance(scenes, list) or not 1 <= len(scenes) <= scene_cap:
        _error(f"الحزمة تحتاج 1–{scene_cap} مشهدًا يغطي كل الأقسام.")
    ala_mahl = doc["template"] == "ala_mahl"
    # V22 authors 2–4 pictures; keep existing 5–6-picture ZIPs readable.
    if ala_mahl and not 2 <= len(scenes) <= 6:
        _error("على مهل يحتاج 2–4 صور قصصية؛ حزم 5–6 صور القديمة ما زالت مدعومة.")
    seen: set[str] = set()
    assets: set[str] = set()
    if music_asset:
        assets.add(music_asset)
    prior = (-1, -1)
    covered = set()
    for scene in scenes:
        _object(scene, {"id", "section_id", "asset", "start_text", "motion", "transition"}, {"stock", "board"}, field="scene")
        if three_per_scene:
            stock_spec = scene.get("stock")
            if not (isinstance(stock_spec, dict)
                    and isinstance(stock_spec.get("urls"), list)
                    and len(stock_spec["urls"]) == 3
                    and "url" not in stock_spec):
                _error(f"المشهد {scene['id']}: يلزم ثلاثة روابط stock.urls؛ الصورة asset احتياطية.")
            if "board" in scene:
                _error("حزمة الروابط الثلاثة: لا تستخدم لوحة بدل مشهد الستوك.")
        if "stock" in scene:
            _stock(scene["stock"], fmt, doc["template"])
        if "board" in scene:
            if "stock" in scene:
                _error("المشهد إما لوحة أو ستوك، لا الاثنان.")
            for board_item in _board(scene["board"], fmt)["items"]:
                if board_item.get("asset"):
                    assets.add(board_item["asset"])
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
    board_scenes = [scene for scene in scenes if "board" in scene]
    if board_scenes:
        from .board_scene import BOARD_MAX_SCENES

        if len(board_scenes) > BOARD_MAX_SCENES[fmt]:
            _error(f"عدد اللوحات: {BOARD_MAX_SCENES[fmt]} كحد أقصى لهذا النوع.")
        if scenes[0] in board_scenes:
            _error("أول مشهد (الهوك) لا يكون لوحة؛ ابدأ بصورة ثم ضع اللوحة بعده.")
        for index, scene in enumerate(scenes):
            if "board" not in scene:
                continue
            text = spoken_text(texts[scene["section_id"]])
            begin = text.index(scene["start_text"])
            following = [s for s in scenes[index + 1:] if s["section_id"] == scene["section_id"]]
            end = text.index(following[0]["start_text"]) if following else len(text)
            span = text[begin:end]
            last = -1
            for board_item in scene["board"]["items"]:
                needle = " ".join(board_item["at_text"].split())
                if span.count(needle) != 1:
                    _error(f"at_text «{needle[:40]}» يجب أن يطابق نصًا واحدًا حرفيًا داخل مشهد اللوحة {scene['id']}.")
                position = span.index(needle)
                if position <= last:
                    _error(f"رتّب عناصر اللوحة {scene['id']} بحسب ورودها في الكلام.")
                last = position
    stock_count = sum(1 for scene in scenes if "stock" in scene)
    pinned_urls = [
        url for scene in scenes
        for url in ([scene["stock"]["url"]] if "url" in scene.get("stock", {})
                    else scene.get("stock", {}).get("urls", []))
    ]
    pinned_ids = [parse_stock_url(url)[:3:2] for url in pinned_urls]
    if len(pinned_ids) != len(set(pinned_ids)):
        _error("stock.url مكرر: لكل مشهد مقطع أو صورة مختلفة.")
    stock_cap = len(scenes) if three_per_scene else (ALA_MAHL_STOCK_MAX if ala_mahl else STOCK_MAX_SCENES.get(fmt, 0))
    if stock_count > stock_cap:
        _error(f"لقطات الستوك: {stock_cap} كحد أقصى لهذا النوع.")
    if ala_mahl and stock_count:
        # A section without its own picture continues the previous picture. A stock clip
        # there would jump back to its still fallback in the middle of the story.
        for index, scene in enumerate(scenes):
            if "stock" not in scene:
                continue
            number = int(scene["section_id"][1:])
            last_of_section = index + 1 == len(scenes) or scenes[index + 1]["section_id"] != scene["section_id"]
            if last_of_section and f"s{number + 1}" in texts and f"s{number + 1}" not in {x["section_id"] for x in scenes}:
                _error("لقطة الستوك في على مهل لا تكون آخر مشهد قبل قسم يكمل الصورة نفسها.")
    if ala_mahl and scenes[0]["section_id"] != "s1":
        _error("أول صورة في على مهل يجب أن تبدأ من بداية s1.")
    if not ala_mahl and covered != set(texts):
        _error("يجب أن تغطي الصور جميع أقسام النص.")
    # Image uniqueness is independent of the optional MP3 attachment.
    # Never count a music bed as an Ala Mahl scene image.
    if ala_mahl and len({scene["asset"] for scene in scenes}) != len(scenes):
        _error("على مهل يحتاج صورة أصلية مختلفة لكل مشهد؛ لا تكرر الصورة.")
    cover = _object(doc["cover"], {"asset", "text"}, field="cover")
    _text(cover["text"], "cover.text", 100, empty=True)
    if len(cover["text"].split()) > 6:
        _error("نص الغلاف لا يتجاوز 6 كلمات.")
    if cover["asset"] is not None:
        assets.add(_asset(cover["asset"]))
    _publishing(doc["publishing"], "publishing")
    derivative = doc.get("derivative")
    if derivative is not None:
        _object(derivative, {"start_text", "end_text"}, {"cover", "publishing"}, field="derivative")
        if fmt == "short":
            _error("المشتق متاح للفيلم والبودكاست فقط.")
        for key in ("start_text", "end_text"):
            _text(derivative[key], f"derivative.{key}", 1500)
        if "cover" in derivative:
            # The derived Short's own cover, designed by the editor (portrait 9:16), never a frame crop.
            dcover = _object(derivative["cover"], {"asset", "text"}, field="derivative.cover")
            _text(dcover["text"], "derivative.cover.text", 100, empty=True)
            if len(dcover["text"].split()) > 6:
                _error("نص غلاف المشتق لا يتجاوز 6 كلمات.")
            assets.add(_asset(dcover["asset"]))
        if "publishing" in derivative:
            _publishing(derivative["publishing"], "derivative.publishing")
        _derivative_avoids_boards(doc, texts, derivative)
        # Invalid/missing anchors are intentionally NOT a parent-production gate.
        # The optional extractor records the precise skip reason after parent success.
    if available is not None and assets - available:
        _error("صور أساسية مفقودة: " + "، ".join(sorted(assets - available)))
    return doc


def _derivative_avoids_boards(doc: Mapping[str, Any], texts: Mapping[str, str], derivative: Mapping[str, Any]) -> None:
    """A vertical derivative would crop a 16:9 board; it must not cover one."""
    boards = [s for s in doc["scenes"] if "board" in s]
    if not boards:
        return
    offsets, whole = {}, ""
    for sid, text in texts.items():
        offsets[sid] = len(whole)
        whole += spoken_text(text) + " "
    first = whole.find(" ".join(derivative["start_text"].split()))
    last = whole.find(" ".join(derivative["end_text"].split()))
    if first < 0 or last < 0:
        return
    last += len(" ".join(derivative["end_text"].split()))
    for scene in boards:
        text = spoken_text(texts[scene["section_id"]])
        begin = text.index(scene["start_text"])
        nxt = [s for s in doc["scenes"] if s["section_id"] == scene["section_id"] and text.index(s["start_text"]) > begin]
        end = text.index(nxt[0]["start_text"]) if nxt else len(text)
        lo, hi = offsets[scene["section_id"]] + begin, offsets[scene["section_id"]] + end
        if lo < last and hi > first:
            _error("مقطع derivative يقطع لوحة توضيحية؛ اجعله خارج مشاهد اللوحات.")


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
                from .owner_youtube_music import ASSET as MUSIC_ASSET
                if name != "production.json" and not (_ASSET.fullmatch(name) or name == MUSIC_ASSET):
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
            from .owner_youtube_music import ASSET as MUSIC_ASSET, verify_audio
            selected_music = doc["editing"].get("music") == "youtube_audio_library"
            if selected_music:
                if not verify_audio(files[MUSIC_ASSET], doc["editing"]["track_id"]):
                    _error("الملف الصوتي ليس النسخة الموسيقية المعتمدة أو بصمته مختلفة.")
            elif MUSIC_ASSET in files:
                _error("لا يسمح بملف موسيقى دون اختيار صريح في editing.")
            from PIL import Image
            for name, content in files.items():
                if name in {"production.json", MUSIC_ASSET}:
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
