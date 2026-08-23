from __future__ import annotations

import os
from pathlib import Path

from scripts.run_control_production import execute_bundle, load_control_request
from scripts.unified_delivery import finalize_release_manifest, write_delivery_manifest


def _github_output(name: str, value: str) -> None:
    target = (os.environ.get("GITHUB_OUTPUT") or "").strip()
    if not target:
        print(f"{name}={value}")
        return
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def main() -> None:
    if (os.environ.get("CONTROL_PLANE_PRODUCTION_ENABLED") or "false").strip().lower() != "true":
        raise RuntimeError("Telegram production wrapper requires explicit control-plane production enablement")
    request_path = Path(os.environ.get("ISCO_CONTROL_REQUEST_PATH") or "")
    expected = (os.environ.get("ISCO_CONTROL_REQUEST_SHA256") or "").strip()
    release_tag = (os.environ.get("ISCO_CONTROL_RELEASE_TAG") or "").strip()
    if not str(request_path) or not expected or not release_tag:
        raise RuntimeError("Telegram production wrapper requires exact request path, hash, and release tag")

    request = load_control_request(request_path, expected)
    runtime_root = Path(os.environ.get("RUNNER_TEMP") or ".") / "isco-control-runtime" / str(request.get("request_id") or "request")
    output = execute_bundle(request, runtime_root=runtime_root).resolve()
    final_video = output / "final.mp4"
    if not final_video.is_file() or final_video.stat().st_size <= 1024 * 1024:
        raise RuntimeError("Telegram production completed without a valid final.mp4")

    manifest = output / "delivery-manifest.json"
    if manifest.is_file():
        finalize_release_manifest(manifest, repository=os.environ.get("GITHUB_REPOSITORY", "mymusa79-tech/Isco-Video-Runner"), release_tag=release_tag)
    else:
        write_delivery_manifest(
            output,
            repository=os.environ.get("GITHUB_REPOSITORY", "mymusa79-tech/Isco-Video-Runner"),
            release_tag=release_tag,
            request=request,
            short_assets=[],
            output=manifest,
        )

    marker = Path(os.environ.get("RUNNER_TEMP") or ".") / "telegram-production-output.txt"
    marker.write_text(str(output), encoding="utf-8")
    _github_output("output_dir", str(output))
    _github_output("final_video", str(final_video))
    _github_output("delivery_manifest", str(manifest))
    _github_output("release_tag", release_tag)
    print(f"Telegram control production completed: request={request.get('request_id')} output={output}")


if __name__ == "__main__":
    main()
