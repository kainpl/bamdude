"""Update schemas treat an explicit null for a NOT NULL column as "unchanged" (``schemas/not_null.py``).

A PATCH/PUT body whose fields are ``X | None = None`` and whose route applies what was sent with
``model_dump(exclude_unset=True)`` + ``setattr`` wrote the null into the column and answered 500.
The guard keeps every such body on the base, so a new route cannot bring the 500 back.
"""

from __future__ import annotations

import typing

from fastapi.routing import APIRoute
from pydantic import Field

from backend.app.schemas.not_null import IgnoresNullForRequiredColumns, _table

# PATCH/PUT bodies with a field that accepts null which do not update a row of their own, and why.
NOT_A_ROW_UPDATE = {
    "AppSettingsUpdate": "key/value settings; the effective configuration is validated and a null is a 422",
    "GlobalVirtualPrinterUpdate": "the retired settings door; refuses every per-printer field",
    "ManyfoldConnectionUpdate": "stored as settings through the credential resolver",
    "LinkTagRequest": "links a tag through its own rules; a null is its own 'no value'",
    "SpoolTagLinkRequest": "a Spoolman spool, not a database row",
    "SpoolmanInventoryUpdate": "a Spoolman spool, not a database row",
    "SpoolmanFilamentPatch": "a Spoolman filament, not a database row",
    "SlicerSettingUpdate": "a Bambu Cloud setting, not a database row",
    "InboxSubscriptionsUpdate": "replaces the user's subscription list; null means every event",
    "KProfileNote": "upserts or deletes a note; the row key is resolved by the route, never set from the body",
    "LabelCassetteIn": "upserts a cassette field by field; the name is assigned only when given",
    "WorkingWrite": "an order's working plan, written by its own service",
    "PartImageChoiceIn": "a part's picture choice, written by its own service",
    # Refuse a null themselves, with a 422 their own tests pin -- "unchanged" would hide the mistake.
    "DryingScheduleUpdate": "every rule field but latest_start refuses null (422)",
    "CustomerUpdate": "name, kind and contacts refuse null (422)",
    "CameraUpdate": "refuses null for its required fields (422); its secrets go through the credential resolver",
    "ProjectLineUpdate": "quantity and sort_order refuse null (422)",
    "ProjectUpdate": "name, status, priority and the auto-eject pair refuse null (422)",
    "ProductPartUpdate": "its required fields refuse null (422)",
    "ProductUpdate": "its required fields refuse null (422)",
    "VariantGroupUpdate": "its required fields refuse null (422)",
    "VariantOptionUpdate": "its required fields refuse null (422)",
    "VirtualPrinterUpdate": "the nozzle pair refuses null (422); the route reads None as 'not sent' for the rest",
    "GitBackupConfigUpdate": "the credential resolver decides every field, a null included",
}


def _accepts_null(info) -> bool:
    annotation = info.annotation
    return annotation is typing.Any or type(None) in typing.get_args(annotation)


def _bodies():
    from backend.app.main import app

    for route in app.routes:
        if not (isinstance(route, APIRoute) and route.methods & {"PATCH", "PUT"} and route.body_field):
            continue
        schema = route.body_field.field_info.annotation
        if isinstance(schema, type) and hasattr(schema, "model_fields"):
            yield schema, route.path


def test_every_row_update_body_ignores_null_for_required_columns():
    unguarded = set()
    for schema, path in _bodies():
        if issubclass(schema, IgnoresNullForRequiredColumns) or schema.__name__ in NOT_A_ROW_UPDATE:
            continue
        if not any(_accepts_null(info) for info in schema.model_fields.values()):
            continue  # every field refuses null by itself: a 422, never a write
        unguarded.add(f"{schema.__name__} ({path})")
    assert sorted(unguarded) == []


def test_the_exemptions_are_live():
    names = {schema.__name__ for schema, _ in _bodies()}
    assert set(NOT_A_ROW_UPDATE) <= names, "an exemption names a body no route takes any more"


def test_every_declared_table_exists_and_shares_a_column_with_its_schema():
    seen = {schema for schema, _ in _bodies() if issubclass(schema, IgnoresNullForRequiredColumns)}
    assert seen
    for schema in seen:
        columns = set(_table(schema.__orm_table__).c.keys())  # KeyError for a misspelt table
        assert columns & set(schema.model_fields), (
            f"{schema.__name__} names {schema.__orm_table__}, none of whose columns it has"
        )


class _PlugUpdate(IgnoresNullForRequiredColumns):
    __orm_table__ = "smart_plugs"

    name: str | None = None  # NOT NULL column
    username: str | None = None  # nullable column
    note_for_client: str | None = None  # no column at all
    # NOT NULL column; routes write it under the field name, clients may send the alias
    off_delay_minutes: int | None = Field(default=None, alias="offDelay")


def test_null_for_a_not_null_column_counts_as_not_sent():
    update = _PlugUpdate(name=None, username="x")
    assert update.model_dump(exclude_unset=True) == {"username": "x"}


def test_null_for_a_nullable_column_clears_it():
    update = _PlugUpdate(username=None)
    assert update.model_dump(exclude_unset=True) == {"username": None}


def test_null_for_a_field_with_no_column_is_left_alone():
    update = _PlugUpdate(note_for_client=None)
    assert update.model_dump(exclude_unset=True) == {"note_for_client": None}


def test_a_field_sent_under_its_alias_is_checked_by_its_column():
    assert _PlugUpdate.model_validate({"offDelay": None}).model_dump(exclude_unset=True) == {}


def test_leaving_a_field_out_is_not_null():
    assert _PlugUpdate.model_validate({"username": "x"}).model_dump(exclude_unset=True) == {"username": "x"}
