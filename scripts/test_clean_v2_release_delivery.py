from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from scripts import clean_v2_release_delivery as delivery


class FakeRunner:
    def __init__(self, direct_url: str):
        self.direct_url = direct_url
        self.calls: list[list[str]] = []
        self.uploaded_names: list[str] = []

    def __call__(self, args, text=True, capture_output=True):
        del text, capture_output
        command = list(args)
        self.calls.append(command)
        if command[:3] == ["gh", "release", "view"]:
            return subprocess.CompletedProcess(command, 1, "", "release not found")
        if command[:3] == ["gh", "release", "create"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["gh", "release", "upload"]:
            if len(command) > 4:
                self.uploaded_names.append(Path(command[4]).name)
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["gh", "api"]:
            base = self.direct_url.rsplit("/", 1)[0]
            names = self.uploaded_names or ["final.mp4"]
            body = {
                "assets": [
                    {
                        "name": name,
                        "browser_download_url": self.direct_url if name == "final.mp4" else f"{base}/{name}",
                    }
                    for name in names
                ]
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(body), "")
        return subprocess.CompletedProcess(command, 1, "", "unexpected command")


class CleanV2ReleaseDeliveryTests(unittest.TestCase):
    def _output(self, root: Path, name: str = "") -> Path:
        out = root / name if name else root
        out.mkdir(parents=True, exist_ok=True)
        (out / "final.mp4").write_bytes(b"video-bytes")
        (out / "final-master-qc.json").write_text(
            json.dumps({"status": "pass"}),
            encoding="utf-8",
        )
        (out / "run-manifest.json").write_text(
            json.dumps({"status": "pass", "topic": "موضوع الاختبار"}, ensure_ascii=False),
            encoding="utf-8",
        )
        (out / "cover.jpg").write_bytes(b"cover-bytes")
        (out / "plan.json").write_text(
            json.dumps(
                {
                    "title": "عنوان الاختبار",
                    "promise": "وعد واضح ومفيد",
                    "sections": [
                        {"id": "s1", "heading": "البداية", "cover_text": "ابدأ هنا"},
                        {"id": "s2", "heading": "التحول", "cover_text": "الفكرة الأهم"},
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return out

    def test_release_asset_direct_url_is_sent_to_telegram(self):
        direct = "https://github.com/example/repo/releases/download/tag/final.mp4"
        runner = FakeRunner(direct)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            delivery, "send_message", return_value=True
        ) as send:
            delivery.publish_one(
                root=self._output(Path(tmp)),
                kind="short",
                topic="",
                delivery_key="short-final-one",
                repository="example/repo",
                target_sha="a" * 40,
                run_id="123",
                run_attempt="1",
                run=runner,
            )

        self.assertTrue(any(call[:3] == ["gh", "release", "create"] for call in runner.calls))
        self.assertTrue(any(call[:3] == ["gh", "release", "upload"] for call in runner.calls))
        self.assertEqual(send.call_args.kwargs["button_text"], "🎥 مشاهدة/تحميل الفيديو")
        self.assertEqual(send.call_args.kwargs["button_url"], direct)

    def test_podcast_reuses_one_release_and_sends_optional_short_button(self):
        direct = "https://github.com/example/repo/releases/download/tag/final.mp4"
        runner = FakeRunner(direct)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            delivery, "send_message", return_value=True
        ) as send:
            root = self._output(Path(tmp))
            (root / "podcast-short.mp4").write_bytes(b"short-video")
            (root / "podcast-short-qc.json").write_text(
                json.dumps({"status": "pass"}),
                encoding="utf-8",
            )
            (root / "podcast-short-cover.jpg").write_bytes(b"short-cover")
            (root / "podcast-short.json").write_text(
                json.dumps({"status": "pass", "section_id": "s2"}),
                encoding="utf-8",
            )
            result = delivery.publish_one(
                root=root,
                kind="podcast",
                topic="حلقة خارج النص",
                delivery_key="telegram",
                repository="example/repo",
                target_sha="d" * 40,
                run_id="321",
                run_attempt="1",
                run=runner,
            )

        uploads = [call for call in runner.calls if call[:3] == ["gh", "release", "upload"]]
        self.assertEqual(len(uploads), 3)
        self.assertIn("short_browser_download_url", result)
        self.assertIn("package_browser_download_url", result)
        self.assertEqual(send.call_count, 3)
        self.assertEqual(send.call_args_list[2].kwargs["button_text"], "⚡ مشاهدة/تحميل الشورت")


    def test_long_reuses_one_release_and_sends_optional_derived_short(self):
        direct = "https://github.com/example/repo/releases/download/tag/final.mp4"
        runner = FakeRunner(direct)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            delivery, "send_message", return_value=True
        ) as send:
            root = self._output(Path(tmp))
            (root / "long-short.mp4").write_bytes(b"short-video")
            (root / "long-short-qc.json").write_text(
                json.dumps({"status": "pass"}),
                encoding="utf-8",
            )
            (root / "long-short-cover.jpg").write_bytes(b"short-cover")
            (root / "long-short.json").write_text(
                json.dumps({"status": "pass", "section_id": "s2"}),
                encoding="utf-8",
            )
            result = delivery.publish_one(
                root=root,
                kind="long",
                topic="فيديو طويل",
                delivery_key="telegram",
                repository="example/repo",
                target_sha="e" * 40,
                run_id="654",
                run_attempt="1",
                run=runner,
            )

        uploads = [call for call in runner.calls if call[:3] == ["gh", "release", "upload"]]
        self.assertEqual(len(uploads), 3)
        self.assertIn("short_browser_download_url", result)
        self.assertIn("package_browser_download_url", result)
        self.assertEqual(send.call_count, 3)
        self.assertEqual(send.call_args_list[2].kwargs["button_text"], "⚡ مشاهدة/تحميل الشورت")

    def test_bundle_is_one_long_delivery_with_optional_derived_short(self):
        runner = FakeRunner("https://github.com/example/repo/releases/download/tag/final.mp4")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            delivery, "send_message", return_value=True
        ) as send:
            root = Path(tmp)
            film = self._output(root, "film")
            (film / "long-short.mp4").write_bytes(b"short-video")
            (film / "long-short-qc.json").write_text(
                json.dumps({"status": "pass"}),
                encoding="utf-8",
            )
            (film / "long-short-cover.jpg").write_bytes(b"short-cover")
            (film / "long-short.json").write_text(
                json.dumps({"status": "pass", "section_id": "s2"}),
                encoding="utf-8",
            )
            results = delivery.deliver(
                output_root=root,
                scope="bundle",
                topic="موضوع الحزمة",
                delivery_key="telegram",
                repository="example/repo",
                target_sha="b" * 40,
                run_id="456",
                run_attempt="1",
                run=runner,
            )
        self.assertEqual([item["kind"] for item in results], ["long"])
        self.assertEqual(send.call_count, 3)
        self.assertIn("short_browser_download_url", results[0])
        self.assertIn("package_browser_download_url", results[0])


    def test_one_click_package_contains_publish_ready_assets_for_podcast(self):
        direct = "https://github.com/example/repo/releases/download/tag/final.mp4"
        runner = FakeRunner(direct)
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            delivery, "send_message", return_value=True
        ) as send:
            root = self._output(Path(tmp))
            (root / "podcast-short.mp4").write_bytes(b"short-video")
            (root / "podcast-short-qc.json").write_text(
                json.dumps({"status": "pass"}),
                encoding="utf-8",
            )
            (root / "podcast-short-cover.jpg").write_bytes(b"short-cover")
            (root / "podcast-short.json").write_text(
                json.dumps({"status": "pass", "section_id": "s2"}),
                encoding="utf-8",
            )
            result = delivery.publish_one(
                root=root,
                kind="podcast",
                topic="حلقة خارج النص",
                delivery_key="telegram",
                repository="example/repo",
                target_sha="f" * 40,
                run_id="999",
                run_attempt="1",
                run=runner,
            )
            package = root / "publish-package.zip"
            self.assertTrue(package.is_file())
            with zipfile.ZipFile(package) as archive:
                names = set(archive.namelist())
                expected = {
                    "video.mp4",
                    "cover.jpg",
                    "publish.txt",
                    "publish.json",
                    "derived-short.mp4",
                    "derived-short-cover.jpg",
                    "derived-short-publish.txt",
                    "derived-short-publish.json",
                    "README.txt",
                    "package-manifest.json",
                }
                self.assertTrue(expected.issubset(names))
                publish = json.loads(archive.read("publish.json").decode("utf-8"))
            self.assertIn("خارج النص", publish["title"])
            self.assertIn("#خارج_النص", publish["hashtags"])
            self.assertIn("package_browser_download_url", result)
            self.assertEqual(
                send.call_args_list[0].kwargs["button_text"],
                "📦 تحميل حزمة النشر كاملة",
            )


    def test_delivery_is_blocked_before_release_when_final_master_qc_is_not_pass(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(
            delivery, "send_message"
        ) as send:
            root = Path(tmp)
            (root / "final.mp4").write_bytes(b"video")
            (root / "final-master-qc.json").write_text(
                json.dumps({"status": "failed"}),
                encoding="utf-8",
            )
            runner = FakeRunner("https://example.invalid/final.mp4")
            with self.assertRaisesRegex(RuntimeError, "final_master_qc is not PASS"):
                delivery.publish_one(
                    root=root,
                    kind="long",
                    topic="موضوع",
                    delivery_key="long",
                    repository="example/repo",
                    target_sha="c" * 40,
                    run_id="789",
                    run_attempt="1",
                    run=runner,
                )
        self.assertEqual(runner.calls, [])
        send.assert_not_called()

    def test_every_clean_v2_workflow_that_runs_pipeline_and_checks_final_mp4_uses_shared_delivery(self):
        workflows = sorted(Path(".github/workflows").glob("clean-v2-*.yml"))
        producers = []
        for path in workflows:
            text = path.read_text(encoding="utf-8")
            if "python -m clean_v2" in text and "final.mp4" in text:
                producers.append(path)
                self.assertIn(
                    "python scripts/clean_v2_release_delivery.py deliver",
                    text,
                    msg=f"{path} does not use unified final-video delivery",
                )
        self.assertEqual(
            {path.name for path in producers},
            {
                "clean-v2-minimal-e2e.yml",
                "clean-v2-short-cohort.yml",
                "clean-v2-short-final-one.yml",
                "clean-v2-telegram-production.yml",
            },
        )


if __name__ == "__main__":
    unittest.main()
