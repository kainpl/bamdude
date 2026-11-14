"""Give a product's printed parts a sliced source in a test (spec product-part-variant-combinations, п. 10).

A product with variant groups takes a new product line only when every printed part of the
chosen kit has a sliced plate in a file outside the trash (``services/combination_gate.py``).
A fixture that orders such a product, and is not itself about slicing, calls :func:`slice_parts`
once: one sliced file whose plate holds one instance of each printed part, linked to the product
as a whole-file plate. Call it after the parts are flushed (it reads their ``name_key``).
"""

from collections.abc import Iterable

from backend.app.models.library import LibraryFile
from backend.app.models.product import ProductPart, ProductPlate


async def slice_parts(db, product_id: int, parts: Iterable[ProductPart]) -> LibraryFile:
    """A sliced plate yielding each printed part of ``parts`` once, on ``product_id``; flushes."""
    names = [part.name_key for part in parts if part.kind == "printed"]
    sliced = LibraryFile(
        filename=f"sliced-{product_id}.gcode.3mf",
        file_path=f"sliced-{product_id}.gcode.3mf",
        file_size=1,
        file_type="gcode",
        file_metadata={
            "plates": [
                {
                    "index": 1,
                    "printable_objects": {str(i): name for i, name in enumerate(names, start=1)},
                    "print_time_seconds": 600,
                }
            ]
        },
    )
    db.add(sliced)
    await db.flush()
    db.add(ProductPlate(product_id=product_id, library_file_id=sliced.id, plate_index=0))
    await db.flush()
    return sliced
