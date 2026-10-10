"""A product with a sliced, rendered plate -- the farm every part-image test starts from (plan E4).

Built through the ORM, not through ``sync_product_for_file``: the sync would seed an automatic
part for the unclaimed object ("Cube") and the tests need it unassigned. Render rows are written
directly: the one-writer guard of part_renders scans backend/app, not the tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from backend.app.core.config import settings
from backend.app.models.library import LibraryFile
from backend.app.models.plate_render import PlateRender, PlateRenderObject
from backend.app.models.product import Product, ProductPart, ProductPlate
from backend.app.services.part_names import canonicalize, name_key
from backend.app.services.part_render_protocol import RENDERED, RENDERER_VERSION
from backend.app.services.product_sync import wanted_plate_indices

SHA = "a" * 64
RESULT = "f" * 32
OBJECTS = {11: "Body", 12: "Body", 13: "Lid", 14: "Cube"}
METHODS = {11: "toolpath", 12: "toolpath", 13: "top_mask", 14: "toolpath"}


@dataclass
class Farm:
    product: Product
    body: ProductPart
    lid: ProductPart
    file: LibraryFile
    render: PlateRender | None
    plate: int  # the linked plate the objects are on: 1, or 0 for a single-plate file


def png(path: Path, side: int = 8) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGBA", (side, side), (200, 50, 50, 255)).save(path, "PNG")


def part(product_id: int, name: str, *, kind: str = "printed", aliases: list[str] | None = None) -> ProductPart:
    key = name_key(canonicalize(name)) if kind == "printed" else f"purchased:{name.lower()}"
    return ProductPart(
        product_id=product_id,
        kind=kind,
        name=name,
        name_key=key,
        aliases=(aliases or [key]) if kind == "printed" else None,
        qty_per_unit=1,
    )


def metadata(objects: dict[int, str], plate: int = 1, *, empty_plates: tuple[int, ...] = ()) -> dict:
    plates = [{"index": plate, "printable_objects": {str(k): v for k, v in objects.items()}}]
    plates += [{"index": index, "printable_objects": {}} for index in empty_plates]
    return {"plates": plates}


async def add_render(
    db,
    *,
    sha: str = SHA,
    plate: int = 1,
    status: str = "ready",
    methods: dict[int, str] | None = None,
    result_dir: str | None = None,
    write_files: bool = False,
) -> PlateRender:
    """A render row and its objects. A ``ready`` row gets ``RESULT`` unless told otherwise; any
    other status has no result unless one is passed (a ``pending`` re-render keeps its old one)."""
    from backend.app.services.part_renders import result_path

    methods = METHODS if methods is None else methods
    render = PlateRender(
        file_sha256=sha,
        plate_index=plate,
        renderer_version=RENDERER_VERSION,
        status=status,
        phase="render",
        result_dir=result_dir or (RESULT if status == "ready" else None),
    )
    db.add(render)
    await db.flush()
    if render.result_dir:
        db.add_all(
            PlateRenderObject(render_id=render.id, identify_id=i, method=m, width=8, height=8, tools=[])
            for i, m in methods.items()
        )
        if write_files:
            directory = result_path(settings.part_renders_dir, sha, RENDERER_VERSION, plate, render.result_dir)
            for i, m in methods.items():
                if m in RENDERED:
                    png(directory / f"{i}.lg.png", 64)
                    png(directory / f"{i}.sm.png", 16)
    return render


async def rendered_farm(
    db,
    *,
    objects: dict[int, str] | None = None,
    methods: dict[int, str] | None = None,
    status: str | None = "ready",
    sha: str = SHA,
    write_files: bool = False,
    name: str = "Lamp",
    on_disk: bool = False,
    single_plate: bool = False,
) -> Farm:
    """``on_disk`` puts real bytes behind the row and keys everything on THEIR hash -- what an
    export needs: it hashes the bytes it packs, and a row without bytes is left out.

    The links are the ones the product sync itself makes (``product_sync.wanted_plate_indices``),
    so an export and an import -- which links through that sync -- land on the same plates
    (consilium E4.2-R1). The default farm is a two-plate file: the objects on plate 1 and an
    empty plate 2, both linked. ``single_plate`` is the commonest real file, one numbered plate,
    which the sync links as plate 0, the whole file; its render is plate 0's too.
    """
    if on_disk:
        import hashlib

        from backend.app.api.routes.library import to_absolute_path

        content = f"part-thumbnails fixture {name}".encode()
        sha = hashlib.sha256(content).hexdigest()
        path = to_absolute_path(f"library/{name.lower()}.gcode.3mf")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    file = LibraryFile(
        filename=f"{name.lower()}.gcode.3mf",
        file_path=f"library/{name.lower()}.gcode.3mf",
        file_type="gcode",
        file_size=10,
        file_hash=sha,
        file_metadata=metadata(objects or OBJECTS, empty_plates=() if single_plate else (2,)),
    )
    linked = sorted(wanted_plate_indices(file.file_metadata))  # (0,) or (1, 2) -- the sync's own answer
    product = Product(name=name, library_files=[file])
    db.add(product)
    await db.flush()
    body, lid = part(product.id, "Body"), part(product.id, "Lid")
    db.add_all(
        [body, lid, *(ProductPlate(product_id=product.id, library_file_id=file.id, plate_index=p) for p in linked)]
    )
    render = None
    if status is not None:
        render = await add_render(db, sha=sha, plate=linked[0], status=status, methods=methods, write_files=write_files)
    await db.commit()
    return Farm(product=product, body=body, lid=lid, file=file, render=render, plate=linked[0])
