"""A product part's picture (spec part-thumbnails §11; plan E4, task 31).

Two media routes an ``<img>`` loads with a media token (the part's effective picture; one plate
object's picture), the candidates and the three write doors of the part editor, and the
product's re-render request. Everything they decide is services/part_images.py's.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.api.routes._workshop_rights import bind_workshop_credentials  # not core.auth (consilium E4-R1)
from backend.app.api.routes.library import file_name_visible
from backend.app.api.routes.products import _part_out
from backend.app.core.auth import (
    RequirePermission,
    library_name_scope,
    require_media_any_permission,
    require_media_permission,
)
from backend.app.core.database import get_db
from backend.app.core.permissions import Permission
from backend.app.models.product import Product
from backend.app.models.user import User
from backend.app.schemas.part_image import (
    PartImageCandidateOut,
    PartImageChoiceIn,
    PartImagesRerenderIn,
    PartImagesRerenderOut,
)
from backend.app.schemas.product import ProductPartResponse
from backend.app.services import part_images, part_renders
from backend.app.services.product_files import attachment_limit, exceeds_attachment_limit, image_media_type

router = APIRouter(prefix="/product-parts", tags=["products"], dependencies=[Depends(bind_workshop_credentials)])
product_router = APIRouter(prefix="/products", tags=["products"], dependencies=[Depends(bind_workshop_credentials)])

# ``immutable`` only for the URL that names the picture it gets: ``v`` changes with the picture
# (spec §11.1, plan E4, D13). Without it, or with another, the picture is revalidated -- the URL
# is stable across the picture changing, as the product cover's is. ``private``: one farm's data
# behind a token, never a shared cache's.
_KEEP = "private, max-age=31536000, immutable"
_REVALIDATE = "private, no-cache"
Size = Literal["sm", "lg"]


def _picture(path, current: str, asked: str | None) -> FileResponse:
    return FileResponse(
        path,
        media_type=image_media_type(path.name),
        headers={"Cache-Control": _KEEP if asked == current else _REVALIDATE},
    )


@router.get("/{part_id}/image")
async def get_part_image(
    part_id: int,
    v: str | None = None,
    size: Size = "sm",
    db: AsyncSession = Depends(get_db),
    _=Depends(require_media_any_permission(Permission.PRODUCTS_READ, Permission.ORDERS_READ, Permission.STOCK_READ)),
):
    """The part's effective picture -- a label in the catalog, an order and the stock, like the cover."""
    found = await part_images.image_file(db, part_id, size)
    if found is None:
        raise HTTPException(status_code=404, detail="No picture for this part")
    return _picture(found[0], found[1], v)


@product_router.get("/{product_id}/files/{library_file_id}/plates/{plate_index}/objects/{identify_id}/image")
async def get_plate_object_image(
    product_id: int,
    library_file_id: int,
    plate_index: int,
    identify_id: int,
    v: str | None = None,
    size: Size = "sm",
    db: AsyncSession = Depends(get_db),
    _=Depends(require_media_permission(Permission.PRODUCTS_READ)),
):
    """One object of a plate the product links: a foreign, unlinked or missing file is the same 404."""
    found = await part_images.instance_file(db, product_id, library_file_id, plate_index, identify_id, size)
    if found is None:
        raise HTTPException(status_code=404, detail="No picture for this object")
    return _picture(found[0], found[1], v)


@router.get("/{part_id}/image-candidates", response_model=list[PartImageCandidateOut])
async def get_part_image_candidates(
    part_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User | None = RequirePermission(Permission.PRODUCTS_READ),
):
    scope = await library_name_scope(request, db, user)
    found = await part_images.candidates(db, part_id, lambda file: file_name_visible(file, user, scope))
    if found is None:
        raise HTTPException(status_code=404, detail="Part not found")
    return found


@router.put("/{part_id}/image", response_model=ProductPartResponse)
@part_images.attach(editor=True)
async def set_part_image(
    part_id: int,
    data: PartImageChoiceIn,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRODUCTS_UPDATE),
):
    return await _part_out(db, await part_images.set_choice(db, part_id, data.source, data.instance))


@router.post("/{part_id}/image/photo", response_model=ProductPartResponse)
@part_images.attach(editor=True)
async def upload_part_photo(
    part_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRODUCTS_UPDATE),
):
    if exceeds_attachment_limit(getattr(file, "size", None)):
        raise HTTPException(status_code=413, detail=f"A photo may be at most {attachment_limit()} bytes")
    return await _part_out(db, await part_images.set_photo(db, part_id, await file.read()))


@router.delete("/{part_id}/image/photo", response_model=ProductPartResponse)
@part_images.attach(editor=True)
async def delete_part_photo(
    part_id: int,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRODUCTS_UPDATE),
):
    return await _part_out(db, await part_images.clear_photo(db, part_id))


@product_router.post("/{product_id}/part-images/rerender", response_model=PartImagesRerenderOut)
async def rerender_part_images(
    product_id: int,
    data: PartImagesRerenderIn | None = None,
    db: AsyncSession = Depends(get_db),
    _: User | None = RequirePermission(Permission.PRODUCTS_UPDATE),
):
    """Spec §9.1: this product's failed plates back to the queue; with ``full``, the ready ones too.
    The writer names every product that shares a moved plate (plan E4, D23)."""
    if await db.get(Product, product_id) is None:
        raise HTTPException(status_code=404, detail="Product not found")
    queued = await part_renders.rerender_for_product(db, product_id, full=bool(data and data.full))
    return PartImagesRerenderOut(queued=queued)
