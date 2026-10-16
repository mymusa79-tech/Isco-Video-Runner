"""Deliver only a retried derivative; the parent Release/final stays untouched."""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

from clean_v2.derived_short import attempt, restore_retry_source, warning_text
from scripts.clean_v2_release_delivery import _upload_and_get_direct_url
from scripts.telegram_clean_v2_notify import send_message


def retry(output: Path, tag: str, repository: str) -> dict:
    if not re.fullmatch(r"clean-v2-final-(?:long|podcast)-[A-Za-z0-9_-]{1,130}", tag):
        raise ValueError("unsupported frozen parent release tag")
    try:
        restore_retry_source(output / "derivative-source.zip", output)
        report = attempt(output)
    except Exception as exc:
        report = {"status": "failed", "primary_status": "SUCCESS", "tts_calls_added": 0,
                  "reason": f"{type(exc).__name__}:{str(exc)[:180]}"}
    if report["status"] == "pass":
        try:
            video = output / report["file"]
            url = _upload_and_get_direct_url(tag=tag, video=video, repository=repository, run=subprocess.run)
            send_message("⚡ الشورت المشتق جاهز من الفيديو المحفوظ. لم يُولّد صوت جديد.",
                         button_text="⚡ مشاهدة/تحميل المشتق", button_url=url)
        except Exception as exc:
            report = {**report, "status": "failed", "reason": f"derivative_delivery:{type(exc).__name__}:{str(exc)[:150]}"}
    if report["status"] != "pass":
        send_message(warning_text(report))
    return report


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--release-tag", required=True)
    args = parser.parse_args()
    print(json.dumps(retry(args.output, args.release_tag, os.environ["GITHUB_REPOSITORY"]), ensure_ascii=False))


if __name__ == "__main__":
    main()
