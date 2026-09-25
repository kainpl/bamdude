"""The one answer both inventory modes give when a tag is already taken (upstream #3110).

Linking an RFID tag lives in two routes — ``inventory.py`` for the built-in
inventory and ``spoolman_inventory.py`` for Spoolman mode — and they refused a
duplicate with two different sentences, only one of which named the spool
holding the tag. Both now raise the structured detail built here, in BamDude's
refusal shape: ``error`` is the code a client branches on, ``message`` the
sentence the boundary translates (``i18n/api_errors``), and ``spool_id`` /
``field`` say which spool holds which identifier.
"""

from __future__ import annotations

from typing import Literal

from fastapi import HTTPException

# Which identifier collided: separate columns on a built-in spool, separate
# lengths inside Spoolman's ``extra.tag``.
TagField = Literal["tag_uid", "tray_uuid"]


def tag_already_linked(field: TagField, holder_id: int) -> HTTPException:
    """409 naming the active spool that already carries this tag."""
    return HTTPException(
        status_code=409,
        detail={
            "error": "tag_already_linked",
            # Two whole sentences, not one template with the identifier's name
            # interpolated: the catalog translates a sentence, not a word inside it.
            "message": (
                f"Tag UID is already linked to spool {holder_id}"
                if field == "tag_uid"
                else f"Tray UUID is already linked to spool {holder_id}"
            ),
            "spool_id": holder_id,
            "field": field,
        },
    )
