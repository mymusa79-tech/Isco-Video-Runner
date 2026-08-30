from __future__ import annotations

from dataclasses import dataclass

from scripts.channel_os_memory import AutonomyMode, OperationalPolicy


YOUTUBE_UPLOAD_MODE = "manual_in_youtube_studio"
YOUTUBE_UPLOADER = "user_only"


@dataclass(frozen=True)
class YouTubePublicationContract:
    """Immutable Channel OS view of the live production publication boundary.

    Channel OS may prepare delivery artifacts and metadata, but it never owns a
    YouTube upload or publication action. The human user is the sole uploader.
    """

    upload_mode: str = YOUTUBE_UPLOAD_MODE
    uploader: str = YOUTUBE_UPLOADER
    channel_os_upload_allowed: bool = False
    channel_os_publish_allowed: bool = False


def publication_contract(policy: OperationalPolicy) -> YouTubePublicationContract:
    """Return the sealed publication contract after validating existing firewalls."""
    AutonomyMode(policy.autonomy_mode)
    if policy.require_publish_approval is not True:
        raise RuntimeError("Channel OS publish approval firewall was weakened")
    return YouTubePublicationContract()


def channel_os_youtube_upload_allowed(policy: OperationalPolicy) -> bool:
    """Deliberately impossible in every autonomy mode."""
    return publication_contract(policy).channel_os_upload_allowed


def channel_os_youtube_publish_allowed(policy: OperationalPolicy) -> bool:
    """Deliberately impossible in every autonomy mode."""
    return publication_contract(policy).channel_os_publish_allowed
