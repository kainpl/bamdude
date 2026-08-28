"""Schema-level guarantees for direct-to-device label printing."""

from __future__ import annotations

from backend.app.core.auth import _APIKEY_DENIED_PERMISSIONS, _APIKEY_SCOPE_BY_PERMISSION
from backend.app.core.permissions import DEFAULT_GROUPS, Permission
from backend.app.models.api_key import APIKey
from backend.app.models.label_device import LabelCassette, LabelDevice, LabelJob
from backend.app.schemas.settings import AppSettings

NEW_PERMISSIONS = (
    Permission.LABEL_DEVICES_READ,
    Permission.LABEL_DEVICES_POLL,
    Permission.LABEL_DEVICES_MANAGE,
    Permission.LABEL_JOBS_CREATE,
)


def test_a_device_is_not_enabled_until_somebody_enables_it():
    """⚠️ Pairing, not trust. An API key proves the caller is a bridge; it does
    not decide that this particular printer should be given our labels.
    """
    assert LabelDevice.__table__.c.enabled.default.arg is False


def test_installation_id_is_unique():
    """It is the only thing tying a poll to a row. Two devices answering to one
    id would take each other's jobs.
    """
    assert LabelDevice.__table__.c.installation_id.unique is True


def test_a_bridge_that_answers_is_not_a_printer_that_answers():
    """Two different failures with two different fixes, so two different fields:
    the desktop process can be up while the USB cable is out.
    """
    assert LabelDevice.__table__.c.printer_reachable.default.arg is False
    assert "last_seen_at" in LabelDevice.__table__.c


def test_a_job_starts_queued():
    assert LabelJob.__table__.c.status.default.arg == "queued"


def test_a_job_carries_its_own_picture():
    """⚠️ Rendered at enqueue, never recomputed on claim. The queue can sit for
    hours on a desktop that is switched off, and a job must print what the
    operator previewed even if the spool is renamed in between.
    """
    assert LabelJob.__table__.c.image_png.nullable is False


def test_a_job_names_the_design_but_does_not_depend_on_it():
    """⚠️ Informational only. The stored PNG is what prints; a template edited
    or deleted afterwards must not change or destroy a queued job.
    """
    column = LabelJob.__table__.c.template_id
    assert column.nullable is True
    assert not column.foreign_keys, "a foreign key here would delete history with the design"


def test_a_job_records_the_size_it_was_drawn_at():
    """The device has to know before it prints, and the picture alone cannot say
    — a 1-bit raster has dots, not millimetres.
    """
    assert LabelJob.__table__.c.width_mm.nullable is False
    assert LabelJob.__table__.c.height_mm.nullable is False


def test_deleting_a_device_takes_its_queue_with_it():
    """A job for a printer nobody can reach any more is not history worth keeping."""
    fk = next(iter(LabelJob.__table__.c.device_id.foreign_keys))
    assert fk.ondelete == "CASCADE"


def test_the_cassette_catalogue_is_keyed_by_barcode():
    """It is the only thing the printer knows about its own stock; the size is
    ours to record.
    """
    assert LabelCassette.__table__.c.barcode.unique is True


def test_the_new_key_scope_defaults_off():
    """⚠️ Unlike the can_manage_* columns, which split an existing capability and
    were backfilled per row. This capability is new — nobody had it, so nobody
    loses anything by not being granted it.
    """
    assert APIKey.__table__.c.can_print_labels.default.arg is False


def test_the_subsystem_is_off_by_default():
    """It needs a desktop bridge running somewhere. A farm that has none must not
    be shown a queue nothing will ever drain.
    """
    assert AppSettings().device_labels_enabled is False


def test_every_new_permission_lands_in_exactly_one_api_key_map():
    for perm in NEW_PERMISSIONS:
        in_scope = perm in _APIKEY_SCOPE_BY_PERMISSION
        denied = perm in _APIKEY_DENIED_PERMISSIONS
        assert in_scope != denied, f"{perm} must be in exactly one of the two maps"


def test_adopting_a_device_is_admin_only():
    """It decides that a machine on somebody's desk may receive our labels."""
    assert Permission.LABEL_DEVICES_MANAGE in _APIKEY_DENIED_PERMISSIONS
    assert Permission.LABEL_DEVICES_MANAGE.value not in DEFAULT_GROUPS["Operators"]["permissions"]


def test_polling_uses_its_own_narrow_scope():
    """⚠️ The bridge's key sits on a desktop somewhere. It must not reach the
    library or the inventory — and POLL mutates (it claims a job), so it cannot
    ride the read scope either.
    """
    assert _APIKEY_SCOPE_BY_PERMISSION[Permission.LABEL_DEVICES_POLL] == "can_print_labels"
    assert _APIKEY_SCOPE_BY_PERMISSION[Permission.LABEL_JOBS_CREATE] == "can_print_labels"


def test_an_operator_may_print_and_a_viewer_may_only_look():
    operators = set(DEFAULT_GROUPS["Operators"]["permissions"])
    viewers = set(DEFAULT_GROUPS["Viewers"]["permissions"])
    assert Permission.LABEL_JOBS_CREATE.value in operators
    assert Permission.LABEL_DEVICES_READ.value in operators
    assert Permission.LABEL_DEVICES_READ.value in viewers
    assert Permission.LABEL_JOBS_CREATE.value not in viewers


def test_the_migration_seeds_exactly_what_the_groups_declare():
    """⚠️ Two lists of the same decision. Administrators are not self-healed at
    startup and our migrations are frozen, so a permission in one and not the
    other is a permission that never reaches an existing install.
    """
    from backend.app.migrations.m147_device_direct_labels import (
        ADMIN_PERMISSIONS,
        OPERATOR_PERMISSIONS,
        VIEWER_PERMISSIONS,
    )

    assert set(ADMIN_PERMISSIONS) == {p.value for p in NEW_PERMISSIONS}
    assert set(OPERATOR_PERMISSIONS) <= set(DEFAULT_GROUPS["Operators"]["permissions"])
    assert set(VIEWER_PERMISSIONS) <= set(DEFAULT_GROUPS["Viewers"]["permissions"])
    assert set(OPERATOR_PERMISSIONS) == {
        p.value for p in NEW_PERMISSIONS if p.value in set(DEFAULT_GROUPS["Operators"]["permissions"])
    }


def test_every_scope_column_is_reachable_through_the_api():
    """⚠️ A scope the key form cannot set is a scope nobody can ever have.

    Adding the column, mapping the permission to it and wiring it through the
    schemas are three separate edits, and the first two pass every existing test
    on their own — the bridge simply never authenticates, with a 403 that names
    a scope the UI does not offer. Caught exactly that way.
    """
    from backend.app.models.api_key import APIKey
    from backend.app.schemas.api_key import APIKeyCreate, APIKeyResponse, APIKeyUpdate

    columns = {c.name for c in APIKey.__table__.columns if c.name.startswith("can_")}
    for model in (APIKeyCreate, APIKeyResponse, APIKeyUpdate):
        missing = columns - set(model.model_fields)
        assert not missing, f"{model.__name__} cannot express {sorted(missing)}"
