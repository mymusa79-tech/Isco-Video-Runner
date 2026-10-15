from __future__ import annotations

import unittest

from clean_v2 import owner_film, owner_pack, owner_textfile


class TextFileTests(unittest.TestCase):
    def test_detects_txt_by_name_or_mime(self) -> None:
        for doc in ({"file_name": "a.TXT"}, {"mime_type": "text/plain"}):
            self.assertTrue(owner_textfile.is_text_document({"document": doc}))
        self.assertFalse(owner_textfile.is_text_document({"document": {"file_name": "a.pdf", "mime_type": "application/pdf"}}))
        self.assertFalse(owner_textfile.is_text_document({"text": "x"}))

    def test_arabic_utf8_bom_crlf_and_windows_arabic(self) -> None:
        text = "فيلم جاهز\nعنوان: لماذا نتعب؟\nنص 1: مرحبا"
        self.assertEqual(owner_textfile.decode_text(("﻿" + text).replace("\n", "\r\n").encode("utf-8")), text)
        self.assertEqual(owner_textfile.decode_text(text.encode("utf-16")), text)
        self.assertEqual(owner_textfile.decode_text(text.encode("cp1256")), text)

    def test_empty_and_oversized_files_are_explained(self) -> None:
        with self.assertRaisesRegex(owner_pack.OwnerPackError, "فارغ"):
            owner_textfile.decode_text(b"\xef\xbb\xbf  \n")
        with self.assertRaisesRegex(owner_pack.OwnerPackError, "300KB"):
            owner_textfile.decode_text(b"a" * (owner_textfile.MAX_BYTES + 1))
        with self.assertRaisesRegex(owner_pack.OwnerPackError, "300KB"):
            owner_textfile.message_with_file_text(
                {"document": {"file_id": "x", "file_size": owner_textfile.MAX_BYTES + 1}}, lambda _id: b""
            )

    def test_file_text_parses_exactly_like_a_typed_film_message(self) -> None:
        body = " ".join(["كلمة"] * 70)
        text = "فيلم جاهز\nعنوان: عنوان تجريبي\nنوع: paradox\n" + "\n".join(
            f"نص {i}: جملة أولى. جملة ثانية. {body}." for i in range(1, 6)
        )
        message = owner_textfile.message_with_file_text(
            {"document": {"file_id": "f", "file_size": len(text)}}, lambda _id: text.encode("utf-8")
        )
        self.assertEqual(owner_film.parse_film_message(message["text"]), owner_film.parse_film_message(text))
        self.assertEqual(owner_film.parse_film_message(message["text"])["type"], "paradox")


if __name__ == "__main__":
    unittest.main()
