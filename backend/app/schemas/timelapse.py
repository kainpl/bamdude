"""Schemas for timelapse video processing."""

from typing import Literal

from pydantic import BaseModel, Field

# Which medium records the timelapse, chosen per print like BambuStudio's own
# picker. ``None`` means nobody chose and the printer keeps doing what it did —
# distinct from either value, and the default for every job queued before the
# picker existed. Validated as an enum rather than a free string so a typo
# ("sdcard", "usb") fails at the API edge instead of silently resolving to
# internal three layers down.
TimelapseStorage = Literal["internal", "external"]


class TimelapseInfoResponse(BaseModel):
    """Video metadata response."""

    duration: float = Field(description="Video duration in seconds")
    width: int = Field(description="Video width in pixels")
    height: int = Field(description="Video height in pixels")
    fps: float = Field(description="Frames per second")
    codec: str = Field(description="Video codec name")
    file_size: int = Field(description="File size in bytes")
    has_audio: bool = Field(description="Whether video has audio track")


class ThumbnailResponse(BaseModel):
    """Timeline thumbnail response."""

    thumbnails: list[str] = Field(description="Base64 encoded JPEG thumbnails")
    timestamps: list[float] = Field(description="Timestamp for each thumbnail in seconds")


class ProcessResponse(BaseModel):
    """Processing result response."""

    status: str = Field(description="Processing status: completed, error")
    output_path: str | None = Field(default=None, description="Relative path to output file")
    message: str = Field(description="Status message")
