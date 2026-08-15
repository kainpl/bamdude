"""HMS error descriptions for the browser.

Served rather than bundled: the catalogue is ~4 MB of English across seven
models, and it used to live as a 118 KB constant inside a React component.

One response per model rather than a lookup per error — a printer can report a
dozen faults at once, and the browser holds the answer while the modal is open.
"""

from fastapi import APIRouter, Query

from backend.app.core.auth import RequirePermission
from backend.app.core.permissions import Permission
from backend.app.services.hms_catalogue import descriptions_for

router = APIRouter(prefix="/hms", tags=["hms"])


@router.get("/descriptions")
async def get_hms_descriptions(
    device: str = Query(..., min_length=3, max_length=3, description="First three characters of the serial number"),
    lang: str = Query("en", min_length=2, max_length=5),
    _=RequirePermission(Permission.PRINTERS_READ),
):
    """Every description BamDude knows for one printer model.

    ⚠️ ``device`` is a model prefix, not a printer id — the same key
    ``hms_actions`` uses. An unknown one answers an empty map rather than
    another model's text: 879 codes mean different things on different machines.
    """
    return {
        "device": device.upper(),
        "lang": lang,
        "descriptions": descriptions_for(device.upper(), lang),
    }
