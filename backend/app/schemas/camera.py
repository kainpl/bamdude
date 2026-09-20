"""Standalone cameras — the ones that belong to no printer.

⚠️ The URL is validated HERE, on write, which a printer's ``external_camera_url``
is not: that one is only sanitized when it is opened, so a typo is stored and
surfaces later as a stream that will not start. There is no reason to repeat
that; ``services/external_camera._sanitize_camera_url`` is the same check the
open path runs, so a URL this schema accepts is one the opener accepts too.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

CameraType = Literal["mjpeg", "rtsp", "snapshot", "usb"]

#: 0 / 90 / 180 / 270, the same four the printer's ``camera_rotation`` takes.
ROTATIONS = (0, 90, 180, 270)


def _validated_source(value: str, camera_type: str) -> str:
    """A URL the stream opener will accept, or a USB device path.

    USB sources are device paths (``/dev/video0``), not URLs — they go through
    ``_safe_usb_device_path`` at open time and must not be pushed through a URL
    parser here, which would reject every one of them.
    """
    value = value.strip()
    if not value:
        raise ValueError("camera URL cannot be empty")
    if camera_type == "usb":
        return value
    from backend.app.services.external_camera import _sanitize_camera_url

    if _sanitize_camera_url(value) is None:
        raise ValueError("camera URL must be a valid http(s) or rtsp(s) URL")
    return value


class CameraBase(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    camera_type: CameraType
    url: str = Field(max_length=500)
    snapshot_url: str | None = Field(default=None, max_length=500)
    rotation: int = 0
    enabled: bool = True
    location_id: int | None = None

    @field_validator("name")
    @classmethod
    def _trim_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("camera name cannot be empty")
        return v

    @field_validator("rotation")
    @classmethod
    def _known_rotation(cls, v: int) -> int:
        if v not in ROTATIONS:
            raise ValueError("camera rotation must be 0, 90, 180 or 270")
        return v

    @field_validator("snapshot_url")
    @classmethod
    def _optional_snapshot_url(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        from backend.app.services.external_camera import _sanitize_camera_url

        if _sanitize_camera_url(v.strip()) is None:
            raise ValueError("snapshot URL must be a valid http(s) or rtsp(s) URL")
        return v.strip()


class CameraCreate(CameraBase):
    @field_validator("url")
    @classmethod
    def _source(cls, v: str, info) -> str:
        return _validated_source(v, info.data.get("camera_type", ""))


class CameraUpdate(BaseModel):
    """Every field optional; ``camera_type`` travels with ``url`` when either changes.

    The URL's validity depends on the type (a USB device path is not a URL), so
    the route re-validates the pair against the stored row rather than this
    schema deciding with half the answer.
    """

    name: str | None = Field(default=None, min_length=1, max_length=100)
    camera_type: CameraType | None = None
    url: str | None = Field(default=None, max_length=500)
    snapshot_url: str | None = Field(default=None, max_length=500)
    rotation: int | None = None
    enabled: bool | None = None
    location_id: int | None = None

    @field_validator("name")
    @classmethod
    def _trim_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("camera name cannot be empty")
        return v

    @field_validator("rotation")
    @classmethod
    def _known_rotation(cls, v: int | None) -> int | None:
        if v is not None and v not in ROTATIONS:
            raise ValueError("camera rotation must be 0, 90, 180 or 270")
        return v


class CameraOut(BaseModel):
    id: int
    name: str
    camera_type: str
    url: str
    snapshot_url: str | None = None
    rotation: int = 0
    enabled: bool = True
    location_id: int | None = None
    location_name: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    model_config = {"from_attributes": True}


class CameraWallCamera(BaseModel):
    """What a wall tile draws, and nothing more.

    ⚠️ **No URL.** The kiosk list is fetched with a token that travels in a URL
    on a lobby screen, and an RTSP camera's credentials live inside its URL —
    the same reason ``CamWallPrinter`` carries no serial number. The signed-in
    wall uses this shape too: a tile has no use for the URL either way.
    """

    id: int
    name: str
    rotation: int = 0
    location_id: int | None = None


class CameraTestRequest(BaseModel):
    url: str = Field(max_length=500)
    camera_type: CameraType
