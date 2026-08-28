from __future__ import annotations

import isco_video_agent.media.ffmpeg as media_ffmpeg


# Run #123 never reached media, but its 32-minute pre-media duration exposed a second
# operational risk: a later hung ffmpeg/ffprobe subprocess had no command timeout and
# could consume the remainder of the workflow's 120-minute ceiling. Keep generous
# per-command budgets for an 8-minute 1080p Film while making the failure finite.
# These limits do not change encoding presets, CRF, resolution, loudness, subtitles,
# AV-sync gates, media selection, or any Gold/release decision.
FFMPEG_COMMAND_TIMEOUT_SECONDS = 15 * 60
FFPROBE_COMMAND_TIMEOUT_SECONDS = 2 * 60

# Canonical V4 creates 2-3 source-derived Shorts sequentially after the accepted long
# render. The old 20-minute timeout was per child, so the abnormal-path allowance alone
# could consume 40-60 minutes. A source-derived Moment has no long-form planning or TTS
# pass; ten minutes remains generous while bounding the total abnormal child envelope
# to 20-30 minutes. Isolation remains sequential to avoid provider-quota and file-state
# races; this is a deadline change, not a concurrency/quality change.
SIBLING_SHORT_CHILD_TIMEOUT_SECONDS = 10 * 60


def install_run123_runtime_latency_guard() -> None:
    if getattr(media_ffmpeg, "_ISCO_RUN123_RUNTIME_LATENCY_GUARDED", False):
        return

    original_run = media_ffmpeg._run
    original_check_output = media_ffmpeg._check_output

    def bounded_run(cmd: list[str], **kwargs):
        kwargs.setdefault("timeout", FFMPEG_COMMAND_TIMEOUT_SECONDS)
        return original_run(cmd, **kwargs)

    def bounded_check_output(cmd: list[str], **kwargs) -> str:
        kwargs.setdefault("timeout", FFPROBE_COMMAND_TIMEOUT_SECONDS)
        return original_check_output(cmd, **kwargs)

    media_ffmpeg._run = bounded_run
    media_ffmpeg._check_output = bounded_check_output

    # Import lazily so ordinary module import remains side-effect free. Runtime Closure
    # later calls this same module when it builds the unified long+Shorts bundle.
    from scripts import canonical_v4_bundle

    canonical_v4_bundle.SHORT_CHILD_TIMEOUT_SECONDS = SIBLING_SHORT_CHILD_TIMEOUT_SECONDS
    media_ffmpeg._ISCO_RUN123_RUNTIME_LATENCY_GUARDED = True
    print(
        "Run123 downstream latency guard installed: "
        f"ffmpeg_command_timeout={FFMPEG_COMMAND_TIMEOUT_SECONDS}s "
        f"ffprobe_timeout={FFPROBE_COMMAND_TIMEOUT_SECONDS}s "
        f"sibling_short_child_timeout={SIBLING_SHORT_CHILD_TIMEOUT_SECONDS}s "
        "quality_settings=unchanged"
    )
