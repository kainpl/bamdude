"""The installation's logo for the stream overlay (G09; upstream 67d3e7e86).

Two doors read the same one file (``services/overlay_logo``):

* ``/settings/overlay-logo`` — the signed-in side: the builder in Settings uploads, replaces and removes the
  logo under ``settings:update`` and shows what is saved.
* ``/overlay-branding/logo`` — what OBS loads. OBS has no session, so the overlay it opens carries a long-lived
  ``overlay`` token in its address, and this route takes the same token as the overlay's status feed.

⚠️ That second route is a DELIBERATE EXCEPTION to "no long-lived token opens a picture"
(``40-invariants/inv-media-token-checks-the-resource``; owner, 2026-11-15): the logo is the installation's
branding, not a user's resource — there is no owner or printer to check it against — and the overlay that draws
it is opened without a session. It serves this one file: no path parameter, no directory, no other picture.
"""

from __future__ import annotations

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import Response

from backend.app.core.auth import RequireAnyPermission, RequireOverlayToken, RequirePermission
from backend.app.core.permissions import Permission
from backend.app.services import overlay_logo

router = APIRouter(tags=["overlay-branding"])


def _refuse(reason: str) -> HTTPException:
    """The refusal for an upload that is not a logo — one literal sentence each, for the translation catalog."""
    if reason == "too_large":
        return HTTPException(413, "The logo may be at most 2 MiB")
    if reason == "too_many_pixels":
        return HTTPException(422, "The logo may have at most 4 million pixels")
    if reason == "animated":
        return HTTPException(422, "An animated picture cannot be the logo")
    return HTTPException(422, "The logo must be a PNG, JPEG or WebP image")


def _unavailable() -> HTTPException:
    """The folder cannot be written — a refusal that says where to look, never a bare 500."""
    return HTTPException(
        507, "The overlay-branding folder in the data directory cannot be written; check its permissions"
    )


# Whoever may open an overlay signed in (it draws the camera) or read the settings the builder lives in.
_MAY_SEE = RequireAnyPermission(Permission.SETTINGS_READ, Permission.CAMERA_VIEW)


def _info(logo: overlay_logo.Logo | None) -> dict:
    return {"exists": logo is not None, "revision": logo.revision if logo else None}


def _picture(logo: overlay_logo.Logo | None, *, asked_revision: str | None) -> Response:
    if logo is None:
        raise HTTPException(404, "No overlay logo is saved")
    # One revision's bytes never change, so an address that names the revision may be kept; any other
    # address is asked again. Never ``public``: the address carries a token, or the answer needed a session.
    kept = asked_revision is not None and asked_revision == logo.revision
    return Response(
        logo.content,
        media_type=overlay_logo.MEDIA_TYPE,
        headers={
            "Cache-Control": "private, max-age=86400, immutable" if kept else "private, no-cache",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": 'inline; filename="overlay-logo.png"',
        },
    )


@router.get("/overlay-branding/logo")
async def get_stream_logo(v: str | None = None, _: None = RequireOverlayToken):
    """The logo for an overlay opened with ``?token=`` — the same grant as its status feed."""
    return _picture(await overlay_logo.current(), asked_revision=v)


@router.get("/settings/overlay-logo/info")
async def get_logo_info(_=_MAY_SEE):
    return _info(await overlay_logo.current())


@router.get("/settings/overlay-logo")
async def get_logo(v: str | None = None, _=_MAY_SEE):
    return _picture(await overlay_logo.current(), asked_revision=v)


@router.post("/settings/overlay-logo")
async def upload_logo(file: UploadFile = File(...), _=RequirePermission(Permission.SETTINGS_UPDATE)):
    # One byte more than allowed is enough to know, and all that is read of an oversized upload.
    content = await file.read(overlay_logo.MAX_BYTES + 1)
    try:
        logo = await overlay_logo.save(content)
    except overlay_logo.LogoRefused as refused:
        raise _refuse(refused.reason) from None
    except overlay_logo.LogoStoreUnavailable:
        raise _unavailable() from None
    return _info(logo)


@router.delete("/settings/overlay-logo")
async def delete_logo(_=RequirePermission(Permission.SETTINGS_UPDATE)):
    try:
        await overlay_logo.remove()
    except overlay_logo.LogoStoreUnavailable:
        raise _unavailable() from None
    return _info(None)
