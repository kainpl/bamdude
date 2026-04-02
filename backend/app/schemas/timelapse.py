"""Schemas for timelapse video processing."""

from pydantic import BaseModel, Field


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
