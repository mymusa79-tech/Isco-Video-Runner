from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from scripts.telegram_clean_v2_notify import send_message

TAG_PREFIX = "clean-v2-final-"
Run = Callable[..., subprocess.CompletedProcess[str]]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _slug(value: str, *, fallback: str) -> str:
    text = re.sub(r"[^a-z0-9-]+", "-", str(value or "").strip().casefold())
    text = re.sub(r"-+", "-", text).strip("-")
    return (text or fallback)[:48]


def _target_dirs(output_root: Path, scope: str) -> list[tuple[str, Path]]:
    root = Path(output_root)
    if scope == "bundle":
        return [("long", root / "film"), ("short", root / "short")]
    kind = "short" if scope == "short" else "long"
    if (root / "final.mp4").is_file() or (root / "final-master-qc.json").is_file():
        return [(kind, root)]
    child = root / ("short" if kind == "short" else "film")
    return [(kind, child)]


def _validate_final(root: Path) -> tuple[Path, dict[str, Any]]:
    video = root / "final.mp4"
    qc_path = root / "final-master-qc.json"
    if not video.is_file() or video.stat().st_size <= 0:
        raise RuntimeError(f"final video missing or empty: {video}")
    qc = _read_json(qc_path)
    if str(qc.get("status") or "").casefold() != "pass":
        raise RuntimeError(f"final_master_qc is not PASS: {qc_path}")
    manifest = _read_json(root / "run-manifest.json")
    if manifest and str(manifest.get("status") or "").casefold() != "pass":
        raise RuntimeError("run manifest is not PASS")
    return video, manifest


def _topic_for(root: Path, explicit: str) -> str:
    value = str(explicit or "").strip()
    if value:
        return value
    for name in ("run-manifest.json", "plan.json"):
        data = _read_json(root / name)
        for key in ("topic", "approved_topic", "title"):
            candidate = str(data.get(key) or "").strip()
            if candidate:
                return candidate
    return ""


def _run(
    args: list[str],
    *,
    run: Run,
    check: bool = False,
) -> subprocess.CompletedProcess[str]:
    result = run(args, text=True, capture_output=True)
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"command failed: {args[0]} {args[1]}: {detail[:300]}")
    return result


def _parse_json_output(result: subprocess.CompletedProcess[str], *, label: str) -> dict[str, Any]:
    try:
        payload = json.loads(result.stdout or "")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{label} returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"{label} returned non-object JSON")
    return payload


def _release_tag(*, kind: str, delivery_key: str, run_id: str, run_attempt: str) -> str:
    key = _slug(delivery_key, fallback="delivery")
    suffix = _slug(run_id, fallback="run")
    attempt = _slug(run_attempt, fallback="1")
    return f"{TAG_PREFIX}{kind}-{key}-{suffix}-a{attempt}"


def _release_title(kind: str, topic: str) -> str:
    label = "Short" if kind == "short" else "Long"
    base = f"Clean V2 {label}"
    return f"{base} — {topic}" if topic else base


def _ensure_release(
    *,
    tag: str,
    title: str,
    repository: str,
    target_sha: str,
    run: Run,
) -> None:
    view = _run(
        [
            "gh",
            "release",
            "view",
            tag,
            "--repo",
            repository,
            "--json",
            "tagName,targetCommitish",
        ],
        run=run,
    )
    if view.returncode == 0:
        payload = _parse_json_output(view, label="gh release view")
        if str(payload.get("tagName") or "") != tag:
            raise RuntimeError("existing delivery Release tag mismatch")
        if str(payload.get("targetCommitish") or "").casefold() != target_sha.casefold():
            raise RuntimeError("existing delivery Release points at a different Runner SHA")
        return

    notes = (
        "Clean V2 final-video delivery. "
        "Final Master QC passed before this Release was created. "
        "YouTube publication remains manual."
    )
    _run(
        [
            "gh",
            "release",
            "create",
            tag,
            "--repo",
            repository,
            "--target",
            target_sha,
            "--title",
            title,
            "--notes",
            notes,
        ],
        run=run,
        check=True,
    )


def _upload_and_get_direct_url(
    *,
    tag: str,
    video: Path,
    repository: str,
    run: Run,
) -> str:
    _run(
        [
            "gh",
            "release",
            "upload",
            tag,
            str(video),
            "--repo",
            repository,
            "--clobber",
        ],
        run=run,
        check=True,
    )
    result = _run(
        ["gh", "api", f"repos/{repository}/releases/tags/{tag}"],
        run=run,
        check=True,
    )
    payload = _parse_json_output(result, label="gh api release")
    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise RuntimeError("GitHub Release assets payload is malformed")
    for item in assets:
        if not isinstance(item, dict) or str(item.get("name") or "") != "final.mp4":
            continue
        url = str(item.get("browser_download_url") or "").strip()
        if url.startswith("https://"):
            return url
    raise RuntimeError("GitHub Release final.mp4 browser_download_url is missing")


def publish_one(
    *,
    root: Path,
    kind: str,
    topic: str,
    delivery_key: str,
    repository: str,
    target_sha: str,
    run_id: str,
    run_attempt: str,
    run: Run = subprocess.run,
) -> dict[str, str]:
    video, _ = _validate_final(root)
    resolved_topic = _topic_for(root, topic)
    tag = _release_tag(
        kind=kind,
        delivery_key=delivery_key,
        run_id=run_id,
        run_attempt=run_attempt,
    )
    title = _release_title(kind, resolved_topic)
    _ensure_release(
        tag=tag,
        title=title,
        repository=repository,
        target_sha=target_sha,
        run=run,
    )
    url = _upload_and_get_direct_url(
        tag=tag,
        video=video,
        repository=repository,
        run=run,
    )
    text = "🎥 الفيديو النهائي جاهز"
    if resolved_topic:
        text += f"\nالعنوان: {resolved_topic}"
    if not send_message(
        text,
        button_text="🎥 مشاهدة/تحميل الفيديو",
        button_url=url,
    ):
        print("Telegram direct-video delivery warning: message was not delivered")
    return {
        "kind": kind,
        "topic": resolved_topic,
        "release_tag": tag,
        "browser_download_url": url,
    }


def deliver(
    *,
    output_root: Path,
    scope: str,
    topic: str,
    delivery_key: str,
    repository: str,
    target_sha: str,
    run_id: str,
    run_attempt: str,
    run: Run = subprocess.run,
) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    for kind, root in _target_dirs(output_root, scope):
        results.append(
            publish_one(
                root=root,
                kind=kind,
                topic=topic,
                delivery_key=delivery_key,
                repository=repository,
                target_sha=target_sha,
                run_id=run_id,
                run_attempt=run_attempt,
                run=run,
            )
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified Clean V2 final-video Release delivery")
    parser.add_argument("command", choices=("deliver",))
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--scope", choices=("long", "short", "bundle"), required=True)
    parser.add_argument("--topic", default="")
    parser.add_argument("--delivery-key", required=True)
    args = parser.parse_args()

    repository = str(os.environ.get("GITHUB_REPOSITORY") or "").strip()
    target_sha = str(os.environ.get("GITHUB_SHA") or "").strip()
    run_id = str(os.environ.get("GITHUB_RUN_ID") or "").strip()
    run_attempt = str(os.environ.get("GITHUB_RUN_ATTEMPT") or "1").strip()
    if not repository or not target_sha or not run_id:
        raise RuntimeError("GitHub delivery identity is incomplete")

    results = deliver(
        output_root=args.output_root,
        scope=args.scope,
        topic=args.topic,
        delivery_key=args.delivery_key,
        repository=repository,
        target_sha=target_sha,
        run_id=run_id,
        run_attempt=run_attempt,
    )
    print(json.dumps({"status": "pass", "deliveries": results}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
