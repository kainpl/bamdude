"""Cloud Link camera snapshot — one frame, on the portal's request.

The portal cannot reach a printer's camera: the farm is behind whatever NAT,
firewall or CGNAT the user's home has, and the whole point of the agent is that
nothing has to be opened for it. So "show me the camera" is a *command* — the
portal names a printer and hands over a one-shot upload URL, and the agent
pushes the frame outward to it. The image never travels over the WebSocket:
frames on that link are JSON, small and ordered, and a JPEG among them would
stall every status behind it for the length of the upload.

**This module is the whole camera path and it runs on the reader task.**
:func:`capture_and_upload` is invoked as a post-action by the client loop, after
the ``cmd_result`` has already been sent, and the loop contains every exception
it raises. That containment is the contract, not a courtesy: a print farm has
cameras that are unplugged, printers that are offline and portals mid-deploy,
and none of those may cost the link. What the operator gets instead is a
``cmd:camera_snapshot`` audit row with ``ok=False``.

⚠️ **The arguments are validated in** :mod:`commands` **and nowhere else.**
``printer_id`` and ``upload_url`` arrive as non-empty strings or the command is
refused before this function is ever reached. Re-checking them here would be a
second opinion about the same question, and the two would drift.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

if TYPE_CHECKING:  # pragma: no cover — the annotation only, so this module stays light
    from backend.app.services.cloud_link.uplink import Uplink


async def capture_and_upload(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    uplink: Uplink,
    printer_id: str,
    upload_url: str,
) -> None:
    """Grab one frame from a printer's camera and PUT it at ``upload_url``.

    Args:
        session_factory: Opens a database session. A **factory**, never a live
            session: this runs off the reader task, long after the loop's own
            sessions have closed, and it must not share one with a caller whose
            transaction it cannot see the state of.
        uplink: The link's uplink — the way to the printer manager that holds
            the live camera, without this module growing its own dependency on
            it.
        printer_id: The printer as the portal names it (``UplinkPrinter.id``,
            a string). Already validated non-empty by the dispatcher.
        upload_url: The portal's one-shot destination for the image. Already
            validated non-empty by the dispatcher.

    Raises:
        NotImplementedError: Always, for now — the capture and the upload land
            in the next task. The signature is here because the client loop's
            post-action, its containment and its audit row are testable without
            it, and a caller written against a stub cannot drift from one.
    """
    raise NotImplementedError("Cloud Link camera snapshot capture is not implemented yet")
