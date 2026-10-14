"""Owner scene review for Shorts: state transitions, reply parsing and Telegram sends.

Pure stdlib on purpose: it runs both in the control workflow (no Pillow/ffmpeg) and in
the production workflow right after the run stops for review. The pipeline side lives
in ``clean_v2/scene_review.py``.

State machine (``request["scene_review"]``, outside the immutable request hash):
  collect   -> set when a Short is confirmed; phase 1 runs and stops after the voice
  awaiting  -> review pack recorded; waiting for ONE reply or the auto button
  selected  -> choices saved and phase 2 dispatched; every later reply/button is stale
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

CANDIDATES_PER_BEAT = 6
AUTO_CALLBACK_PREFIX = "sceneauto:"
_PREFIXED_RE = re.compile(r"^\s*(?:مشاهد|مشهد)\s+[0-9٠-٩\-،,\s]+$")
_BARE_RE = re.compile(r"^[0-9٠-٩\-،,\s]+$")
_KEEP = ("provider", "asset_id", "download_url", "source_url", "creator", "creator_url", "query", "media_kind")


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def has_prefix(text: str) -> bool:
    return bool(_PREFIXED_RE.match(str(text or "").strip()))


def is_reply_text(text: str) -> bool:
    """«مشاهد 2 1 0» or bare digits like «5354400» (at least two characters)."""
    value = str(text or "").strip()
    if has_prefix(value):
        return True
    return bool(_BARE_RE.match(value)) and len(re.sub(r"[\s،,]", "", value)) >= 2


def _candidate(raw: Mapping[str, Any]) -> dict[str, Any]:
    return {key: raw.get(key) for key in _KEEP if raw.get(key) not in (None, "")}


def parse_reply(text: str, beats: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """One token per beat: 1-6 candidate, 0 channel background, - keep automatic.

    A wrong token count or an unavailable number raises ValueError, so a typo can
    never dispatch a run.
    """
    digits = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
    tokens = str(text or "").translate(digits).replace("،", " ").replace(",", " ").split()
    if tokens and tokens[0] in {"مشاهد", "مشهد"}:
        tokens = tokens[1:]
    if len(tokens) == 1 and len(beats) > 1 and set(tokens[0]) <= set("0123456-"):
        tokens = list(tokens[0])
    if len(tokens) != len(beats):
        raise ValueError(f"expected {len(beats)} choices, got {len(tokens)}")
    result: dict[str, dict[str, Any]] = {}
    for beat, token in zip(beats, tokens):
        beat_id = str(beat.get("id"))
        if token == "-":
            continue
        if token == "0":
            result[beat_id] = {"mode": "background"}
            continue
        if not (token.isdigit() and 1 <= int(token) <= CANDIDATES_PER_BEAT):
            raise ValueError(f"invalid choice {token!r}")
        match = next(
            (c for c in beat.get("candidates", []) if int(c.get("n", 0)) == int(token)),
            None,
        )
        if match is None:
            raise ValueError(f"beat {beat_id} has no candidate {token}")
        result[beat_id] = {"mode": "candidate", "candidate": _candidate(match)}
    return result


def _request(state: Mapping[str, Any], request_id: str, request_sha256: str) -> dict[str, Any]:
    request = state.get("requests", {}).get(request_id)
    if not isinstance(request, dict) or request.get("request_sha256") != request_sha256:
        raise RuntimeError("scene review request integrity mismatch")
    return request


def record_review(
    state: dict[str, Any], request_id: str, request_sha256: str, pack: Mapping[str, Any]
) -> dict[str, Any]:
    request = _request(state, request_id, request_sha256)
    review = request.get("scene_review")
    if not isinstance(review, dict) or review.get("status") not in {"collect", "awaiting"}:
        raise RuntimeError("scene review is not collecting for this request")
    beats = []
    for beat in pack.get("beats", []):
        beats.append(
            {
                "id": str(beat.get("id")),
                "index": int(beat.get("index") or len(beats) + 1),
                "role": str(beat.get("role") or ""),
                "text_ar": str(beat.get("text_ar") or ""),
                "sheet": str(beat.get("sheet") or ""),
                "candidates": [
                    {"n": int(c["n"]), **_candidate(c)} for c in beat.get("candidates", [])
                ],
            }
        )
    if not beats or not str(pack.get("review_id") or ""):
        raise RuntimeError("scene review pack is empty")
    review.clear()
    review.update(
        status="awaiting",
        review_id=str(pack["review_id"]),
        beats=beats,
        created_at=utc_now(),
    )
    return request


def find_awaiting(state: Mapping[str, Any]) -> dict[str, Any] | None:
    found = [
        r for r in state.get("requests", {}).values()
        if isinstance(r, dict)
        and isinstance(r.get("scene_review"), dict)
        and r["scene_review"].get("status") == "awaiting"
    ]
    found.sort(key=lambda r: str(r["scene_review"].get("created_at") or ""))
    return found[-1] if found else None


STALE = "⛔ لا توجد مراجعة مشاهد معلّقة الآن (اعتُمدت سابقًا أو لم تبدأ). لم يتغير شيء ولم يبدأ أي تشغيل."


def prepare_choice(
    state: Mapping[str, Any], *, text: str = "", auto_review_id: str = ""
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Validate a reply/button WITHOUT mutating state. Returns (request, choices, auto)."""
    request = find_awaiting(state)
    if request is None:
        raise LookupError(STALE)
    review = request["scene_review"]
    if auto_review_id:
        if auto_review_id != review.get("review_id"):
            raise LookupError("⛔ هذا الزر قديم؛ لا يطابق مراجعة المشاهد الحالية. لم يتغير شيء.")
        return request, {}, True
    try:
        return request, parse_reply(text, review["beats"]), False
    except ValueError as exc:
        raise ValueError(
            f"⚠️ لم أفهم الاختيار ({exc}). أرسل رقمًا لكل مشهد بالترتيب: "
            f"{len(review['beats'])} مشاهد. مثال: مشاهد "
            + " ".join(["1"] * len(review["beats"]))
            + "\n(1-6 مرشح، 0 خلفية القناة، - إبقاء الاختيار الآلي). لم يبدأ أي تشغيل."
        ) from exc


def commit_choice(request: dict[str, Any], choices: dict[str, Any], auto: bool) -> None:
    review = request["scene_review"]
    review["status"] = "selected"
    review["choices"] = choices
    review["auto_all"] = bool(auto)
    review["selected_at"] = utc_now()


def export_choices(state: Mapping[str, Any], request_id: str, output: Path) -> bool:
    review = state.get("requests", {}).get(request_id, {}).get("scene_review")
    if not isinstance(review, dict) or review.get("status") != "selected":
        return False
    payload = {
        "schema_version": 1,
        "review_id": review.get("review_id"),
        "auto_all": bool(review.get("auto_all")),
        "choices": review.get("choices") or {},
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    return True


# --- Telegram sends (runner side, right after phase 1) ---------------------------

def _post(method: str, fields: Mapping[str, str], file: tuple[str, Path] | None = None) -> bool:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        return False
    url = f"https://api.telegram.org/bot{token}/{method}"
    if file is None:
        request = urllib.request.Request(
            url,
            data=json.dumps(dict(fields), ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
    else:
        boundary = uuid.uuid4().hex
        parts = []
        for key, value in fields.items():
            parts.append(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
            )
        name, path = file
        parts.append(
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"; filename="{path.name}"\r\n'
                f"Content-Type: {mimetypes.guess_type(path.name)[0] or 'image/jpeg'}\r\n\r\n"
            ).encode()
            + path.read_bytes()
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())
        request = urllib.request.Request(
            url,
            data=b"".join(parts),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
    with urllib.request.urlopen(request, timeout=60) as response:
        return bool(json.loads(response.read().decode("utf-8")).get("ok"))


def send_review(output: Path) -> int:
    pack = json.loads((output / "scene-review.json").read_text(encoding="utf-8"))
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    beats = pack["beats"]
    total = len(beats)
    for beat in beats:
        count = len(beat["candidates"])
        photos = sum(c.get("media_kind") == "photo" for c in beat["candidates"])
        caption = (
            f"🎬 مشهد {beat['index']}/{total}"
            + (f" — {beat['role']}" if beat.get("role") else "")
            + f"\n«{beat.get('text_ar') or ''}»\n"
            + (f"الخيارات: {photos} صورة و{count - photos} فيديو (IMG/VID).\n" if count else "")
            + (f"اختر 1–{count}، أو 0 للخلفية، أو - لإبقاء الاختيار الآلي."
               if count else "لا مرشحين بملاءمة موثقة لهذا المشهد: اختر 0 لخلفية القناة، أو - للاختيار الآلي غير المراجع.")
        )[:1000]
        sheet = output / "scene-review" / str(beat.get("sheet") or "")
        if beat.get("sheet") and sheet.is_file():
            ok = _post("sendPhoto", {"chat_id": chat, "caption": caption}, ("photo", sheet))
        else:
            ok = _post("sendMessage", {"chat_id": chat, "text": caption})
        if not ok:
            print(f"scene review send failed for {beat['id']}")
            return 1
    example = " ".join(["1"] * total)
    text = (
        f"✅ جاهز: {total} مشاهد للمراجعة. الصوت محفوظ ولن يُعاد توليده.\n\n"
        "أرسل رسالة واحدة برقم لكل مشهد بالترتيب (الأرقام فقط تكفي)، مثال:\n"
        f"{example}\n"
        "(1–6 مرشح، 0 خلفية القناة، - إبقاء الاختيار الآلي). تُعتمد الرسالة مرة واحدة فقط.\n\n"
        "أو اضغط الزر لإنتاج الفيديو بالاختيار الآلي دون مراجعة."
    )
    keyboard = {"inline_keyboard": [[{
        "text": "⚡ أنتج بالاختيار الآلي",
        "callback_data": f"{AUTO_CALLBACK_PREFIX}{pack['review_id']}",
    }]]}
    ok = _post("sendMessage", {"chat_id": chat, "text": text, "reply_markup": keyboard})
    return 0 if ok else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    send = sub.add_parser("send")
    send.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    return send_review(args.output)


if __name__ == "__main__":
    raise SystemExit(main())
