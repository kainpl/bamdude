"""Update schemas treat an explicit null for a column that cannot be empty as "unchanged"."""

from typing import Any, ClassVar

from pydantic import BaseModel, model_validator


class IgnoresNullForRequiredColumns(BaseModel):
    """Base for update (PATCH/PUT) schemas that are applied to one ORM row (upstream 8e5116fe).

    Their fields are mostly ``X | None = None`` so a request can leave them out, and the API's
    contract is that null means "unchanged": most routes apply a field only when it is not None,
    and the OpenAPI schema allows null. Routes that apply what was sent with
    ``model_dump(exclude_unset=True)`` instead wrote the null into a NOT NULL column and answered
    500. This drops such a null before validation, so the field counts as not sent on every
    route; a null for a column that may be empty still clears it, and a field that is no column
    of the table is left to the schema and its route.

    ``__orm_table__`` names the table the schema writes to. It is looked up when a request is
    validated, so schemas do not import the models. ``tests/unit/test_not_null_update_schemas.py``
    requires this base on every PATCH/PUT body, or a listed reason.
    """

    __orm_table__: ClassVar[str]
    #: Fields whose null is left for the schema's own validator, which refuses it with a 422.
    __keeps_null__: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="before")
    @classmethod
    def _ignore_null_for_required_columns(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        columns = _table(cls.__orm_table__).c
        by_key = {(info.alias or name): name for name, info in cls.model_fields.items()} | {
            name: name for name in cls.model_fields
        }
        return {
            key: value
            for key, value in data.items()
            if not (
                value is None
                and key in by_key
                and by_key[key] not in cls.__keeps_null__
                and by_key[key] in columns
                and not columns[by_key[key]].nullable
            )
        }


def _table(name: str):
    from backend.app.core.database import Base, import_all_models

    if name not in Base.metadata.tables:
        import_all_models()
    return Base.metadata.tables[name]
