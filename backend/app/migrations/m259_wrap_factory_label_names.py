"""Give untouched factory label names a second line where the stock has room."""

from sqlalchemy import select, update

from backend.app.models.label_template import LabelTemplate
from backend.app.services.label_seed import BUILTIN_TEMPLATES, _roomy_elements

version = 259
name = "wrap_factory_label_names"


async def upgrade(conn):
    for template in BUILTIN_TEMPLATES:
        width, height = template["width_mm"], template["height_mm"]
        previous = _roomy_elements(width, height, wrap_name=False)
        if previous == template["elements"]:
            continue
        target = (
            LabelTemplate.builtin_key == template["builtin_key"],
            LabelTemplate.width_mm == width,
            LabelTemplate.height_mm == height,
            LabelTemplate.updated_at.is_(None),
        )
        row = (await conn.execute(select(LabelTemplate.id, LabelTemplate.elements).where(*target))).one_or_none()
        if row is not None and row.elements == previous:
            await conn.execute(
                update(LabelTemplate).where(LabelTemplate.id == row.id, *target).values(elements=template["elements"])
            )
