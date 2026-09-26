from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from typing import Any, Callable

from scripts.telegram_clean_v2_notify import send_message

TAG_PREFIX = "clean-v2-final-"
PUBLISH_PACKAGE_NAME = "publish-package.zip"
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
        return [("long", root / "film")]
    kind = "short" if scope == "short" else ("podcast" if scope == "podcast" else "long")
    if (root / "final.mp4").is_file() or (root / "final-master-qc.json").is_file():
        return [(kind, root)]
    child = root / ("short" if kind == "short" else ("podcast" if kind == "podcast" else "film"))
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
    label = "Outside Text" if kind == "podcast" else ("Short" if kind == "short" else "Long")
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
    asset_name = Path(video).name
    for item in assets:
        if not isinstance(item, dict) or str(item.get("name") or "") != asset_name:
            continue
        url = str(item.get("browser_download_url") or "").strip()
        if url.startswith("https://"):
            return url
    raise RuntimeError(f"GitHub Release asset browser_download_url is missing: {asset_name}")



def _compact(value: object) -> str:
    return " ".join(str(value or "").split()).strip()


def _youtube_title(value: object) -> str:
    return _compact(value)[:100].rstrip()


def _hashtags(kind: str, *, derived_short: bool = False) -> list[str]:
    if derived_short:
        base = ["#نداء_اليقظة", "#وعي", "#Shorts"]
        if kind == "podcast":
            base.insert(0, "#خارج_النص")
        else:
            base.insert(1, "#تطوير_الذات")
        return base
    if kind == "podcast":
        return ["#خارج_النص", "#نداء_اليقظة", "#وعي"]
    if kind == "short":
        return ["#نداء_اليقظة", "#تطوير_الذات", "#وعي", "#Shorts"]
    return ["#نداء_اليقظة", "#تطوير_الذات", "#وعي"]


def _youtube_tags(kind: str, *, derived_short: bool = False) -> list[str]:
    values = ["نداء اليقظة", "وعي"]
    if kind == "podcast":
        values.insert(0, "خارج النص")
    else:
        values.insert(1, "تطوير الذات")
    if derived_short:
        values.append("Shorts")
    return values


def _main_publish_metadata(root: Path, *, kind: str, topic: str) -> dict[str, Any]:
    plan = _read_json(Path(root) / "plan.json")
    raw_title = _compact(plan.get("title") or topic or "نداء اليقظة")
    if kind == "podcast" and "خارج النص" not in raw_title:
        raw_title = f"{raw_title} | خارج النص"
    title = _youtube_title(raw_title)
    promise = _compact(plan.get("promise"))
    resolved_topic = _compact(topic or plan.get("approved_topic") or plan.get("topic"))
    lines: list[str] = []
    if promise:
        lines.append(promise)
    if resolved_topic and resolved_topic not in promise:
        lines.append(f"الموضوع: {resolved_topic}")
    if kind == "podcast":
        lines.append("حلقة من برنامج «خارج النص» على قناة نداء اليقظة.")
    elif kind == "short":
        lines.append("شورت من قناة نداء اليقظة.")
    else:
        lines.append("فيديو من قناة نداء اليقظة.")
    hashtags = _hashtags(kind)
    description = "\n\n".join(lines + [" ".join(hashtags)])
    return {
        "schema_version": 1,
        "kind": kind,
        "title": title,
        "topic": resolved_topic,
        "description": description,
        "hashtags": hashtags,
        "youtube_tags": _youtube_tags(kind),
        "video_file": "video.mp4",
        "cover_file": "cover.jpg",
        "publication_mode": "manual",
    }


def _derived_short_publish_metadata(
    root: Path,
    *,
    parent_kind: str,
    parent: dict[str, Any],
    short_prefix: str,
) -> dict[str, Any]:
    plan = _read_json(Path(root) / "plan.json")
    report = _read_json(Path(root) / f"{short_prefix}.json")
    section_id = _compact(report.get("section_id"))
    selected: dict[str, Any] = {}
    for row in plan.get("sections") or []:
        if isinstance(row, dict) and _compact(row.get("id")) == section_id:
            selected = row
            break
    angle = _compact(selected.get("cover_text") or selected.get("heading"))
    if not angle:
        angle = _compact(parent.get("topic") or parent.get("title") or "مقتطف من الحلقة")
    title = _youtube_title(
        f"{angle} | خارج النص" if parent_kind == "podcast" and "خارج النص" not in angle else angle
    )
    hashtags = _hashtags(parent_kind, derived_short=True)
    description = "\n\n".join(
        [
            f"مقتطف من: {_compact(parent.get('title'))}",
            " ".join(hashtags),
        ]
    )
    return {
        "schema_version": 1,
        "kind": "derived_short",
        "parent_kind": parent_kind,
        "title": title,
        "topic": _compact(parent.get("topic")),
        "description": description,
        "hashtags": hashtags,
        "youtube_tags": _youtube_tags(parent_kind, derived_short=True),
        "video_file": "derived-short.mp4",
        "cover_file": "derived-short-cover.jpg",
        "source_section_id": section_id or None,
        "publication_mode": "manual",
    }


def _publish_text(metadata: dict[str, Any]) -> str:
    hashtags = " ".join(str(item) for item in metadata.get("hashtags") or [])
    tags = ", ".join(str(item) for item in metadata.get("youtube_tags") or [])
    return (
        "العنوان:\n"
        f"{metadata.get('title', '')}\n\n"
        "الوصف:\n"
        f"{metadata.get('description', '')}\n\n"
        "الهاشتاقات:\n"
        f"{hashtags}\n\n"
        "وسوم YouTube:\n"
        f"{tags}\n"
    )


def _extract_cover_from_video(video: Path, output: Path, *, portrait: bool) -> Path:
    width, height = ((1080, 1920) if portrait else (1280, 720))
    output.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        "0.500",
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-vf",
        f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height}",
        "-q:v",
        "2",
        str(output),
    ]
    result = subprocess.run(command, text=True, capture_output=True)
    if result.returncode != 0 or not output.is_file() or output.stat().st_size <= 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(f"publish package cover fallback failed: {detail[:240]}")
    return output


def _ensure_cover(*, video: Path, cover: Path, portrait: bool) -> Path:
    if cover.is_file() and cover.stat().st_size > 0:
        return cover
    return _extract_cover_from_video(video, cover, portrait=portrait)


def _build_publish_package(root: Path, *, kind: str, topic: str) -> tuple[Path, dict[str, Any]]:
    root = Path(root)
    video = root / "final.mp4"
    cover = _ensure_cover(
        video=video,
        cover=root / "cover.jpg",
        portrait=(kind == "short"),
    )
    main_meta = _main_publish_metadata(root, kind=kind, topic=topic)

    stage = root / ".publish-package"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True, exist_ok=True)
    shutil.copy2(video, stage / "video.mp4")
    shutil.copy2(cover, stage / "cover.jpg")
    (stage / "publish.txt").write_text(_publish_text(main_meta), encoding="utf-8")
    (stage / "publish.json").write_text(
        json.dumps(main_meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    derived_included = False
    derived_status = "not_applicable"
    short_prefix = ""
    if kind in {"podcast", "long"}:
        short_prefix = "podcast-short" if kind == "podcast" else "long-short"
        short_video = root / f"{short_prefix}.mp4"
        short_qc = _read_json(root / f"{short_prefix}-qc.json")
        derived_status = _compact(short_qc.get("status")) or "not_generated"
        if (
            short_video.is_file()
            and short_video.stat().st_size > 0
            and derived_status.casefold() == "pass"
        ):
            short_cover = _ensure_cover(
                video=short_video,
                cover=root / f"{short_prefix}-cover.jpg",
                portrait=True,
            )
            short_meta = _derived_short_publish_metadata(
                root,
                parent_kind=kind,
                parent=main_meta,
                short_prefix=short_prefix,
            )
            shutil.copy2(short_video, stage / "derived-short.mp4")
            shutil.copy2(short_cover, stage / "derived-short-cover.jpg")
            (stage / "derived-short-publish.txt").write_text(
                _publish_text(short_meta),
                encoding="utf-8",
            )
            (stage / "derived-short-publish.json").write_text(
                json.dumps(short_meta, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            derived_included = True

    readme_lines = [
        "حزمة نشر نداء اليقظة",
        "",
        "video.mp4 — الفيديو النهائي",
        "cover.jpg — الكفر الجاهز",
        "publish.txt — العنوان والوصف والهاشتاقات والوسوم للنسخ واللصق",
        "publish.json — نفس بيانات النشر بصيغة منظمة",
    ]
    if derived_included:
        readme_lines.extend(
            [
                "",
                "derived-short.mp4 — الشورت المشتق الجاهز",
                "derived-short-cover.jpg — كفر الشورت المشتق",
                "derived-short-publish.txt — عنوان ووصف وهاشتاقات الشورت",
                "derived-short-publish.json — بيانات نشر الشورت المنظمة",
            ]
        )
    readme_lines.extend(["", "النشر إلى YouTube يبقى يدويًا."])
    (stage / "README.txt").write_text("\n".join(readme_lines) + "\n", encoding="utf-8")

    manifest = {
        "schema_version": 1,
        "status": "ready",
        "kind": kind,
        "topic": _compact(topic),
        "main_title": main_meta["title"],
        "derived_short_included": derived_included,
        "derived_short_status": derived_status,
        "files": sorted(path.name for path in stage.iterdir() if path.is_file()),
        "provider_calls_added": 0,
        "publication_mode": "manual",
    }
    (stage / "package-manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    package = root / PUBLISH_PACKAGE_NAME
    package.unlink(missing_ok=True)
    with zipfile.ZipFile(package, "w", compression=zipfile.ZIP_STORED) as archive:
        for path in sorted(stage.iterdir()):
            if path.is_file():
                archive.write(path, arcname=path.name)
    shutil.rmtree(stage, ignore_errors=True)
    if not package.is_file() or package.stat().st_size <= 0:
        raise RuntimeError("publish package zip is missing or empty")
    return package, manifest

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

    # Build the complete zero-cost publishing package before any remote Release
    # side effect. If packaging cannot be completed, Telegram receives nothing
    # partial and the already-produced final video remains untouched.
    package, package_manifest = _build_publish_package(
        root,
        kind=kind,
        topic=resolved_topic,
    )

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

    # final.mp4 remains a direct asset for compatibility and /last history.
    url = _upload_and_get_direct_url(
        tag=tag,
        video=video,
        repository=repository,
        run=run,
    )
    package_url = _upload_and_get_direct_url(
        tag=tag,
        video=package,
        repository=repository,
        run=run,
    )

    short_url = ""
    if kind in {"podcast", "long"} and package_manifest.get("derived_short_included") is True:
        short_prefix = "podcast-short" if kind == "podcast" else "long-short"
        short_video = Path(root) / f"{short_prefix}.mp4"
        try:
            short_url = _upload_and_get_direct_url(
                tag=tag,
                video=short_video,
                repository=repository,
                run=run,
            )
        except Exception as exc:
            # The short is already inside publish-package.zip, so failure of the
            # extra convenience asset must never destroy the complete one-click
            # package.
            print(f"Telegram {short_prefix} direct asset warning: {type(exc).__name__}")

    package_text = (
        "📦 حزمة النشر الكاملة جاهزة\n"
        f"العنوان: {package_manifest.get('main_title') or resolved_topic}\n"
        "داخلها: الفيديو + الكفر + العنوان + الوصف + الهاشتاقات"
    )
    if package_manifest.get("derived_short_included") is True:
        package_text += " + الشورت المشتق + كفره + بيانات نشره"
    if not send_message(
        package_text,
        button_text="📦 تحميل حزمة النشر كاملة",
        button_url=package_url,
    ):
        raise RuntimeError("Telegram complete publish-package delivery failed")

    text = "🎥 الفيديو النهائي جاهز"
    if resolved_topic:
        text += f"\nالعنوان: {resolved_topic}"
    if not send_message(
        text,
        button_text="🎥 مشاهدة/تحميل الفيديو",
        button_url=url,
    ):
        print("Telegram direct-video delivery warning: message was not delivered")

    delivery = {
        "kind": kind,
        "topic": resolved_topic,
        "release_tag": tag,
        "browser_download_url": url,
        "package_browser_download_url": package_url,
    }
    if short_url:
        short_text = (
            "⚡ شورت «خارج النص» جاهز من نفس الحلقة"
            if kind == "podcast"
            else "⚡ شورت جاهز من أهم جزء في الفيديو الطويل"
        )
        if not send_message(
            short_text,
            button_text="⚡ مشاهدة/تحميل الشورت",
            button_url=short_url,
        ):
            print("Telegram derived-short delivery warning: message was not delivered")
        delivery["short_browser_download_url"] = short_url
    return delivery


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
    parser.add_argument("--scope", choices=("long", "short", "bundle", "podcast"), required=True)
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
