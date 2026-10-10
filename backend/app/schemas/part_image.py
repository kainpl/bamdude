"""A product part's picture on the wire (spec part-thumbnails §10.2, §10.4, §11; plan E4)."""

from typing import Literal

from pydantic import BaseModel, Field

from backend.app.services.part_render_protocol import ID_MAX

PinReason = Literal["file_unlinked", "file_trashed", "object_gone", "not_this_part", "not_rendered"]
#: ``product_plates.plate_index`` and the library ids are INTEGER -- 32-bit on PostgreSQL. A number
#: past it is no plate and no file, and binding it would abort the write (consilium E4-R3).
INT32_MAX = 2**31 - 1


class PartImageRef(BaseModel):
    """The effective picture (spec §10.2). ``v`` is its identity, and the media URL carries it."""

    kind: Literal["photo", "render"]
    status: Literal["ready", "pending"]
    v: str | None = None


class InstanceImageRef(BaseModel):
    """One plate object's picture -- the editor's gallery and the unassigned chips (spec §11.3)."""

    library_file_id: int
    plate_index: int
    identify_id: int
    status: Literal["ready", "pending", "missing", "skipped"]
    v: str | None = None


class InstanceKey(BaseModel):
    library_file_id: int = Field(ge=1, le=INT32_MAX)
    plate_index: int = Field(ge=0, le=INT32_MAX)
    identify_id: int = Field(ge=0, le=ID_MAX)


class PinState(BaseModel):
    valid: bool
    reason: PinReason | None = None


class PhotoState(BaseModel):
    present: bool


class ImageChoice(BaseModel):
    """What the operator chose and whether it still holds (spec §10.4)."""

    source: Literal["auto", "instance", "photo"]
    instance: InstanceKey | None = None
    pin: PinState | None = None
    photo: PhotoState | None = None


class PartImageChoiceIn(BaseModel):
    source: Literal["auto", "instance"]
    instance: InstanceKey | None = None


class PartImageCandidateOut(BaseModel):
    """An instance of the part on a linked plate (spec §11.2). ``filename`` is ``None`` for a file
    the caller may not see in the library -- the picture is the product's, the name is not."""

    library_file_id: int
    filename: str | None
    hidden: bool
    plate_index: int
    identify_id: int
    method: str | None
    reason: str | None
    plate_status: Literal["none", "pending", "ready", "failed", "unavailable"]
    v: str | None
    pinned: bool


class PartImagesRerenderIn(BaseModel):
    full: bool = False


class PartImagesRerenderOut(BaseModel):
    queued: int
