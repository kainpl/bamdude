"""Read-only plate object preview payload.

Its own module so ``routes/library.py`` and ``routes/archives.py`` can share it
without either schema package importing the other.

No ``skipped`` field, deliberately. Nothing here is skippable — the live Skip
Objects dialog owns that — and an always-false flag is an invitation to grow one.
"""

from pydantic import BaseModel


class PlateObjectItem(BaseModel):
    id: int
    name: str
    # Normalised pick-PNG centroid when ``norm`` is true, millimetres otherwise,
    # None when the object appears in no positional source at all (branch 4 of
    # ``services.plate_markers`` then lays it out on a grid).
    x: float | None = None
    y: float | None = None
    norm: bool = False
    # Where the marker goes, as percentages of the image box. Computed by
    # ``services.plate_markers.marker_position`` — the ONE implementation, read
    # by the web overlay and by the image the Telegram bot draws.
    #
    # ⚠️ Optional only for wire compatibility with a client older than this
    # field. Every route BamDude serves fills it.
    marker: dict[str, float] | None = None


class PlateObjectsResponse(BaseModel):
    plate_index: int
    objects: list[PlateObjectItem]
    bbox_all: list[float] | None = None
    # True when NOT ONE object had a pick-PNG centroid: every marker is on the
    # fallback grid and the layout is plausible-looking fiction.
    positions_approximate: bool = False
    # ``gcode_label_objects AND exclude_object``, read live from the 3MF. The
    # preview explains this rather than hiding itself when it is False.
    skip_objects_supported: bool = False
    # False when ``Metadata/top_{N}.png`` is absent — the modal then shows the
    # list with no image rather than markers over a ¾ render.
    has_top_view: bool = False
