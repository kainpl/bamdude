"""Give a product part its variant bindings in a test (spec product-part-variant-combinations).

A part's bindings are its ``ProductPart.option_links`` rows. ``bind`` writes them from
options, each of which names its own ``id`` and ``group_id``: a flushed
``ProductVariantOption`` for a part that is persisted, or a transient
``ProductVariantOption(id=…, group_id=…)`` for one that never meets a session.

Call it before the part's first flush, or once its links are loaded (an earlier ``bind``, a
query): under async SQLAlchemy, assigning the collection of a part flushed without it raises
``MissingGreenlet``.
"""

from backend.app.models.product import ProductPart
from backend.app.models.product_variant import ProductPartOption, ProductVariantOption


def bind(part: ProductPart, *options: ProductVariantOption) -> ProductPart:
    """Bind ``part`` to exactly ``options`` (none: always in the kit) and return it."""
    part.option_links = [ProductPartOption(option_id=option.id, group_id=option.group_id) for option in options]
    return part
