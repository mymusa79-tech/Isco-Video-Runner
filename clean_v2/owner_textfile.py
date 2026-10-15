"""A .txt file as the owner's message (UTF-8 Arabic), instead of pasting a long text.

The Worker forwards the document; the control script downloads it from Telegram and the
text then follows exactly the same path as a typed message («شورت جاهز», «بودكاست جاهز»,
«فيلم جاهز», «تكملة», «تم»). Nothing here parses or rewrites the text.
"""

from __future__ import annotations

from typing import Any, Mapping

from .owner_pack import OwnerPackError

MAX_BYTES = 300 * 1024
_INVISIBLE = "﻿‎‏‪‫‬⁦⁧⁨⁩"


def is_text_document(message: Mapping[str, Any]) -> bool:
    document = message.get("document") if isinstance(message, Mapping) else None
    if not isinstance(document, Mapping):
        return False
    name = str(document.get("file_name") or "").lower()
    mime = str(document.get("mime_type") or "").lower()
    return name.endswith(".txt") or mime == "text/plain"


def decode_text(data: bytes) -> str:
    """UTF-8 (with or without BOM) first, then UTF-16 with BOM, then Windows Arabic."""
    if len(data) > MAX_BYTES:
        raise OwnerPackError("الملف النصي أكبر من 300KB. قسّمه وأرسل الأجزاء بـ «تكملة».")
    candidates = ("utf-8-sig",)
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        candidates = ("utf-16",)
    for encoding in (*candidates, "cp1256"):
        try:
            text = data.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise OwnerPackError("تعذر قراءة الملف النصي. احفظه بترميز UTF-8 وأعد إرساله.")
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").lstrip(_INVISIBLE + " \n\t").rstrip()
    if not cleaned:
        raise OwnerPackError("الملف النصي فارغ.")
    return cleaned


def message_with_file_text(message: Mapping[str, Any], download) -> dict[str, Any]:
    """Return the message with its text replaced by the file's text."""
    document = message["document"]
    if int(document.get("file_size") or 0) > MAX_BYTES:
        raise OwnerPackError("الملف النصي أكبر من 300KB. قسّمه وأرسل الأجزاء بـ «تكملة».")
    text = decode_text(download(str(document["file_id"])))
    return {**message, "text": text}
