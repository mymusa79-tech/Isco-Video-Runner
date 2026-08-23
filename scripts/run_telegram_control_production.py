from __future__ import annotations

import json
import os
from pathlib import Path

from scripts.run_control_production import execute_bundle, load_control_request
from scripts.unified_delivery import write_delivery_manifest


def _github_output(name: str, value: str) -> None:
    target = (os.environ.get("GITHUB_OUTPUT") or "").strip()
    if not target:
        print(f"{name}={value}")
        return
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def _require_staged_manifest(path: Path) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("Telegram delivery manifest must be a JSON object")
    if data.get("release_state") != "staged":
        raise RuntimeError("Telegram delivery manifest claimed release before GitHub Release creation")
    if data.get("release_tag") is not None or data.get("delivery_url") is not None:
        raise RuntimeError("Staged Telegram delivery manifest already contains release provenance")
    if data.get("publication_performed") is not False or data.get("youtube_publish_mode") != "manual_in_youtube_studio":
        raise RuntimeError("Telegram delivery manifest changed the manual YouTube publication contract")


def main() -> None:
    if (os.environ.get("CONTROL_PLANE_PRODUCTION_ENABLED") or "false").strip().lower() != "true":
        raise RuntimeError("Telegram production wrapper requires explicit control-plane production enablement")
    request_value = (os.environ.get("ISCO_CONTROL_REQUEST_PATH") or "").strip()
    expected = (os.environ.get("ISCO_CONTROL_REQUEST_SHA256") or "").strip()
    release_tag = (os.environ.get("ISCO_CONTROL_RELEASE_TAG") or "").strip()
    if not request_value or not expected or not release_tag:
        raise RuntimeError("Telegram production wrapper requires exact request path, hash, and release tag")
    request_path = Path(request_value)

    request = load_control_request(request_path, expected)
    runtime_root = Path(os.environ.get("RUNNER_TEMP") or ".") / "isco-control-runtime" / str(request.get("request_id") or "request")
    output = execute_bundle(request, runtime_root=runtime_root).resolve()
    final_video = output / "final.mp4"
    if not final_video.is_file() or final_video.stat().st_size <= 1024 * 1024:
        raise RuntimeError("Telegram production completed without a valid final.mp4")

    manifest = output / "delivery-manifest.json"
    if manifest.is_file():
        _require_staged_manifest(manifest)
    else:
        write_delivery_manifest(
            output,
            repository=os.environ.get("GITHUB_REPOSITORY", "mymusa79-tech/Isco-Video-Runner"),
            release_tag=None,
            request=request,
            short_assets=[],
            output=manifest,
        )
        _require_staged_manifest(manifest)

    marker = Path(os.environ.get("RUNNER_TEMP") or ".") / "telegram-production-output.txt"
    marker.write_text(str(output), encoding="utf-8")
    _github_output("output_dir", str(output))
    _github_output("final_video", str(final_video))
    _github_output("delivery_manifest", str(manifest))
    _github_output("release_tag", release_tag)
    print(f"Telegram control production staged: request={request.get('request_id')} output={output}")


if __name__ == "__main__":
    main()
