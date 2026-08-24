"""Cloud Link persistence + authorization ground (Phase 0).

Three tables, one permission, and the store that is their only writer. The
model half comes first and pins the shape; the store half below it drives
``services/cloud_link/store.py``, which is where the settings route, the
connect loop and the command handler all reach these tables from.

The permission tests are the load-bearing half. ``cloud_link:manage`` decides
whether this farm is reachable from outside the LAN, and an API key is a
credential that lives in somebody's automation script — the two must never
meet. ``_APIKEY_DENIED_PERMISSIONS`` is the explicit marker; the allowlist in
``core/auth.py`` is what actually refuses.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.auth import (
    _APIKEY_DENIED_PERMISSIONS,
    _APIKEY_SCOPE_BY_PERMISSION,
    _check_apikey_permissions,
    _resolve_apikey_scope,
)
from backend.app.core.permissions import ALL_PERMISSIONS, PERMISSION_CATEGORIES, Permission
from backend.app.models.cloud_link import CloudLink, CloudLinkAudit, CloudLinkPrinter

# ---------------------------------------------------------------- the models


async def test_the_link_row_round_trips(db_session: AsyncSession):
    db_session.add(CloudLink(id=1, enabled=False, portal_url="https://cloud.bamdude.top"))
    await db_session.commit()

    row = (await db_session.execute(select(CloudLink).where(CloudLink.id == 1))).scalar_one()
    assert row.enabled is False
    assert row.portal_url == "https://cloud.bamdude.top"
    assert row.instance_id is None
    assert row.instance_secret_encrypted is None
    assert row.last_connected_at is None
    assert row.last_error is None
    assert row.revoked is False


async def test_an_untouched_row_is_off_and_points_at_the_public_portal(db_session: AsyncSession):
    """The defaults live on the model, not at the callsite.

    A row created by anything other than the settings route — a migration
    backfill, a test, a future bootstrap — must still come out disabled and
    pointing somewhere real. ``enabled`` defaulting to anything but False
    would mean an upgrade turns the link on for a farm that never asked.
    """
    db_session.add(CloudLink(id=1))
    await db_session.commit()

    row = (await db_session.execute(select(CloudLink).where(CloudLink.id == 1))).scalar_one()
    assert row.enabled is False
    assert row.revoked is False
    assert row.portal_url == "https://cloud.bamdude.top"


async def test_the_secret_and_the_error_hold_more_than_a_short_string(db_session: AsyncSession):
    """Both are TEXT, not VARCHAR(n).

    A Fernet token is long and grows with the key format; a transport error is
    whatever the other end said. Truncation of either is silent on SQLite and
    fatal on PostgreSQL, and the second failure mode is the one that reaches a
    user — as a 500 during pairing.
    """
    long_secret = "gAAAAAB" + "x" * 700
    long_error = "handshake refused: " + "y" * 2000
    db_session.add(CloudLink(id=1, instance_secret_encrypted=long_secret, last_error=long_error))
    await db_session.commit()

    row = (await db_session.execute(select(CloudLink).where(CloudLink.id == 1))).scalar_one()
    assert row.instance_secret_encrypted == long_secret
    assert row.last_error == long_error


async def test_a_printer_is_exposed_by_a_row_and_by_nothing_else(db_session: AsyncSession, printer_factory):
    """Exposure is an allowlist: the printer_id IS the row, so a printer is
    listed once or not at all and there is no second copy to disagree with."""
    printer = await printer_factory(name="Exposed")
    db_session.add(CloudLinkPrinter(printer_id=printer.id))
    await db_session.commit()

    exposed = list((await db_session.execute(select(CloudLinkPrinter.printer_id))).scalars())
    assert exposed == [printer.id]


def test_deleting_a_printer_takes_its_exposure_with_it():
    """``ondelete="CASCADE"`` is decorative on SQLite — this codebase never
    issues ``PRAGMA foreign_keys=ON`` — so the delete-printer route is what
    actually removes the row on the default database. A surviving exposure
    would be a printer id the portal can still ask about after the machine is
    gone."""
    from backend.app.api.routes.printers import PRINTER_CASCADE_MODELS

    assert CloudLinkPrinter in PRINTER_CASCADE_MODELS


async def test_an_audit_row_stamps_itself_and_assumes_success(db_session: AsyncSession):
    db_session.add(CloudLinkAudit(direction="up", kind="status", summary="printer 3 → RUNNING"))
    await db_session.commit()

    row = (await db_session.execute(select(CloudLinkAudit))).scalar_one()
    assert row.id is not None
    assert row.ts is not None
    assert row.direction == "up"
    assert row.kind == "status"
    assert row.summary == "printer 3 → RUNNING"
    assert row.ok is True


async def test_a_failure_is_recorded_as_one(db_session: AsyncSession):
    db_session.add(CloudLinkAudit(direction="down", kind="cmd", summary="pause rejected", ok=False))
    await db_session.commit()

    row = (await db_session.execute(select(CloudLinkAudit))).scalar_one()
    assert row.ok is False
    assert row.direction == "down"


# ------------------------------------------------------------ the permission


def test_the_permission_exists_and_reads_as_a_resource_action():
    assert Permission.CLOUD_LINK_MANAGE.value == "cloud_link:manage"
    assert "cloud_link:manage" in ALL_PERMISSIONS


def test_the_permission_is_offered_in_the_group_editor():
    """A permission absent from PERMISSION_CATEGORIES cannot be granted to a
    custom group through the UI — it exists only for Administrators, silently."""
    categorised = {p for perms in PERMISSION_CATEGORIES.values() for p in perms}
    assert Permission.CLOUD_LINK_MANAGE in categorised


def test_the_permission_is_denied_to_api_keys():
    """Admin-only by explicit marker AND by absence from the allowlist.

    An API key is automation's credential. Managing the cloud link decides
    whether this farm answers to something outside the LAN and mints the
    instance secret that lets it — a decision for a person at the settings
    page, never for a script holding a long-lived token.
    """
    assert Permission.CLOUD_LINK_MANAGE in _APIKEY_DENIED_PERMISSIONS
    assert Permission.CLOUD_LINK_MANAGE not in _APIKEY_SCOPE_BY_PERMISSION
    assert _resolve_apikey_scope(Permission.CLOUD_LINK_MANAGE.value) is None


def test_no_api_key_can_manage_the_link_however_scoped():
    from backend.tests.unit.test_apikey_permission_allowlist import _ALL_SCOPE_FLAGS, _key

    every_scope = _key(**dict.fromkeys(_ALL_SCOPE_FLAGS, True))
    with pytest.raises(HTTPException) as ei:
        _check_apikey_permissions(every_scope, [Permission.CLOUD_LINK_MANAGE.value])
    assert ei.value.status_code == 403
    assert "administrative" in ei.value.detail


# ------------------------------------------------------------- the migration


def test_the_migration_declares_its_version_and_name():
    from backend.app.migrations import m155_cloud_link as m

    assert m.version == 155
    assert m.name == "cloud_link"


async def _legacy_db(path):
    """A database as it looked before this migration."""
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}")
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE printers (id INTEGER PRIMARY KEY, name TEXT)"))
    return engine


async def test_the_migration_creates_the_three_tables(tmp_path):
    from backend.app.migrations import m155_cloud_link as m
    from backend.app.migrations.helpers import table_exists

    engine = await _legacy_db(tmp_path / "m155.db")
    async with engine.begin() as conn:
        await m.upgrade(conn)
        for table in ("cloud_link", "cloud_link_printers", "cloud_link_audit"):
            assert await table_exists(conn, table), f"{table} missing"
    await engine.dispose()


async def test_the_migration_is_safe_to_run_twice(tmp_path):
    """Guarded by table_exists, like every other create in this codebase — a
    re-run on a DB that already has the tables must not throw."""
    from backend.app.migrations import m155_cloud_link as m

    engine = await _legacy_db(tmp_path / "m155_twice.db")
    async with engine.begin() as conn:
        await m.upgrade(conn)
        await m.upgrade(conn)
    await engine.dispose()


async def test_the_migrated_tables_carry_exactly_the_columns_the_models_declare(tmp_path):
    """The two places that define a table must agree, and this is the guard.

    Fresh installs get the tables from ``Base.metadata.create_all``; existing
    ones get them from this migration. The column set is read off the models
    rather than typed out here on purpose — a column added to a model and
    forgotten in the migration is exactly the drift that produces a schema
    working only for whoever installed on the right day, and a hardcoded list
    would have to be remembered too.
    """
    from backend.app.migrations import m155_cloud_link as m
    from backend.app.migrations.helpers import get_table_columns

    engine = await _legacy_db(tmp_path / "m155_ddl.db")
    async with engine.begin() as conn:
        await m.upgrade(conn)
        for model in (CloudLink, CloudLinkPrinter, CloudLinkAudit):
            declared = {c.name for c in model.__table__.columns}
            migrated = set(await get_table_columns(conn, model.__tablename__))
            assert migrated == declared, f"{model.__tablename__}: {declared ^ migrated}"
    await engine.dispose()


async def test_the_migrated_ddl_defaults_match_the_models(tmp_path):
    """A row inserted naming nothing optional comes out the same either way.

    The defaults are the half that a column-name comparison cannot see, and
    they are the half that decides whether an upgraded farm wakes up with the
    link switched on.
    """
    from backend.app.migrations import m155_cloud_link as m

    engine = await _legacy_db(tmp_path / "m155_defaults.db")
    async with engine.begin() as conn:
        await m.upgrade(conn)
        await conn.execute(text("INSERT INTO printers (id, name) VALUES (7, 'p')"))
        await conn.execute(text("INSERT INTO cloud_link (id) VALUES (1)"))
        await conn.execute(text("INSERT INTO cloud_link_printers (printer_id) VALUES (7)"))
        await conn.execute(text("INSERT INTO cloud_link_audit (direction, kind, summary) VALUES ('up', 'k', 's')"))

        enabled, portal_url, revoked = (
            await conn.execute(text("SELECT enabled, portal_url, revoked FROM cloud_link"))
        ).one()
        ok, ts = (await conn.execute(text("SELECT ok, ts FROM cloud_link_audit"))).one()

    assert not enabled, "an upgrade must never switch the link on"
    assert not revoked
    assert portal_url == "https://cloud.bamdude.top"
    assert ok, "an audit row is a success unless it says otherwise"
    assert ts is not None, "the row stamps itself"
    await engine.dispose()


async def test_the_migration_seeds_the_permission_to_administrators(test_engine):
    """Administrators are not self-healed at startup and our migrations are
    frozen — a permission not seeded here is a permission nobody ever has on
    an upgraded install."""
    from backend.app.migrations import m155_cloud_link as m
    from backend.app.models.group import Group

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        db.add(Group(name="Administrators", description="", permissions=["printers:read"], is_system=True))
        db.add(Group(name="Operators", description="", permissions=["printers:read"], is_system=True))
        db.add(Group(name="Homebrew", description="", permissions=["printers:read"], is_system=False))
        await db.commit()

    await m.seed(factory)

    async with factory() as db:
        rows = dict((await db.execute(select(Group.name, Group.permissions))).all())
    assert "cloud_link:manage" in rows["Administrators"]
    assert "printers:read" in rows["Administrators"], "the seed appends, it does not replace"
    assert "cloud_link:manage" not in rows["Operators"], "operators do not open the farm to the internet"
    assert "cloud_link:manage" not in rows["Homebrew"], "custom groups are the admin's to edit"


async def test_seeding_twice_does_not_duplicate_the_entry(test_engine):
    from backend.app.migrations import m155_cloud_link as m
    from backend.app.models.group import Group

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as db:
        db.add(Group(name="Administrators", description="", permissions=[], is_system=True))
        await db.commit()

    await m.seed(factory)
    await m.seed(factory)

    async with factory() as db:
        perms = (await db.execute(select(Group.permissions).where(Group.name == "Administrators"))).scalar_one()
    assert perms.count("cloud_link:manage") == 1


# ------------------------------------------------------------------ the store
#
# Everything below drives ``services/cloud_link/store.py``. The store is the
# only writer of these three tables, which is what lets the settings route, the
# connect loop and the command handler each hold their own session without
# having to agree on a transaction.


async def test_asking_for_the_config_creates_the_singleton_and_then_reuses_it(db_session: AsyncSession):
    """Get-or-create, because every caller needs a row and none of them owns
    creating it — the settings page, the connect loop and a fresh install all
    arrive at an empty table and must not each make their own."""
    from backend.app.services.cloud_link.store import get_config

    first = await get_config(db_session)
    assert first.id == 1
    assert first.enabled is False
    assert first.portal_url == "https://cloud.bamdude.top"

    second = await get_config(db_session)
    assert second.id == 1

    rows = list((await db_session.execute(select(CloudLink.id))).scalars())
    assert rows == [1], "a singleton that can be created twice is two answers to one question"


async def test_a_caller_that_looked_too_early_reads_what_the_winner_wrote(db_session: AsyncSession):
    """Two callers racing to create the singleton on a fresh install.

    The startup connect loop and the first settings request both find an empty
    table and both insert ``id = 1``; the database tells the loser so. Forced
    here by blinding one lookup — the primary-key violation, the rollback and
    the re-read that follow are all real. Without that recovery this is a 500
    on a first boot that nobody can reproduce afterwards, because every later
    attempt finds the row.
    """
    from backend.app.services.cloud_link.store import get_config

    await db_session.execute(text("INSERT INTO cloud_link (id) VALUES (1)"))
    await db_session.commit()

    real_get = db_session.get
    looked_early = []

    async def blind_the_first_look(*args, **kwargs):
        if not looked_early:
            looked_early.append(True)
            return None
        return await real_get(*args, **kwargs)

    db_session.get = blind_the_first_look
    try:
        row = await get_config(db_session)
    finally:
        db_session.get = real_get

    assert looked_early, "the race branch was never entered — this test proves nothing"
    assert row.id == 1
    assert list((await db_session.execute(select(CloudLink.id))).scalars()) == [1]


async def test_the_secret_round_trips_but_never_lands_in_the_row(db_session: AsyncSession):
    """The instance secret is the whole credential — anyone holding it can
    speak for this farm. It goes to disk as Fernet ciphertext and comes back
    only through the store."""
    from backend.app.core.encryption import is_encryption_active
    from backend.app.services.cloud_link.store import get_secret, save_credentials

    assert is_encryption_active(), "the plaintext-fallback path would make the assertion below vacuous"

    secret = "s3cr3t-instance-token-000111"
    await save_credentials(db_session, instance_id="inst_abc", secret=secret)

    row = (await db_session.execute(select(CloudLink).where(CloudLink.id == 1))).scalar_one()
    assert row.instance_id == "inst_abc"
    assert row.instance_secret_encrypted
    assert secret not in row.instance_secret_encrypted

    assert await get_secret(db_session) == secret


async def test_a_fresh_pair_is_a_fresh_start(db_session: AsyncSession):
    """Pairing again clears ``revoked`` and ``last_error``.

    Both describe the credential that was just replaced. Left standing they
    would tell the settings page the farm is revoked while it holds a
    brand-new, working credential — and send the user off to re-pair a link
    that is already paired.
    """
    from backend.app.services.cloud_link.store import get_config, save_credentials

    config = await get_config(db_session)
    config.revoked = True
    config.last_error = "portal said: instance revoked"
    await db_session.commit()

    row = await save_credentials(db_session, instance_id="inst_new", secret="brand-new")
    assert row.revoked is False
    assert row.last_error is None


async def test_there_is_no_secret_before_pairing(db_session: AsyncSession):
    from backend.app.services.cloud_link.store import get_secret

    assert await get_secret(db_session) is None


async def test_reading_the_secret_does_not_create_the_config_row(db_session: AsyncSession):
    """A read stays a read. ``get_secret`` runs on the connect path, which is
    the one place that must be able to answer "are we paired" without writing
    to a database it may be sharing with a migration."""
    from backend.app.services.cloud_link.store import get_secret

    await get_secret(db_session)

    assert list((await db_session.execute(select(CloudLink.id))).scalars()) == []


async def test_clearing_the_credentials_leaves_nothing_to_reconnect_with(db_session: AsyncSession):
    from backend.app.services.cloud_link.store import clear_credentials, get_secret, save_credentials

    await save_credentials(db_session, instance_id="inst_abc", secret="to-be-wiped")
    await clear_credentials(db_session)

    row = (await db_session.execute(select(CloudLink).where(CloudLink.id == 1))).scalar_one()
    assert row.instance_id is None
    assert row.instance_secret_encrypted is None
    assert await get_secret(db_session) is None


async def test_the_publish_set_is_replaced_not_merged(db_session: AsyncSession, printer_factory):
    """The set the user saved IS the set — a merge would mean a printer can
    only ever be added, and unticking one on the settings page would silently
    do nothing."""
    from backend.app.services.cloud_link.store import get_publish_set, set_publish_set

    a = await printer_factory(name="A")
    b = await printer_factory(name="B")
    c = await printer_factory(name="C")

    await set_publish_set(db_session, [a.id, b.id])
    assert await get_publish_set(db_session) == {a.id, b.id}

    await set_publish_set(db_session, [b.id, c.id])
    assert await get_publish_set(db_session) == {b.id, c.id}


async def test_an_empty_publish_set_exposes_nothing(db_session: AsyncSession, printer_factory):
    from backend.app.services.cloud_link.store import get_publish_set, set_publish_set

    printer = await printer_factory(name="A")
    await set_publish_set(db_session, [printer.id])
    await set_publish_set(db_session, [])

    assert await get_publish_set(db_session) == set()


async def test_naming_a_printer_twice_lists_it_once(db_session: AsyncSession, printer_factory):
    """``printer_id`` is the primary key, so a duplicate in the caller's list
    would be an IntegrityError rather than a second row — the store
    de-duplicates so a UI that sends the same id twice is not a 500."""
    from backend.app.services.cloud_link.store import get_publish_set, set_publish_set

    printer = await printer_factory(name="A")
    await set_publish_set(db_session, [printer.id, printer.id])

    assert await get_publish_set(db_session) == {printer.id}


async def test_the_publish_set_is_empty_before_anyone_sets_one(db_session: AsyncSession):
    """Deny by default: enabling the link exposes no machine until a person
    picks one."""
    from backend.app.services.cloud_link.store import get_publish_set

    assert await get_publish_set(db_session) == set()


async def test_an_audit_row_is_written_stamped_and_readable(db_session: AsyncSession):
    from backend.app.services.cloud_link.store import write_audit

    entry = await write_audit(db_session, "up", "status", "printer 3 is RUNNING")

    assert entry.id is not None
    assert entry.ts is not None, "the returned row carries its stamp — callers log it without a second query"
    assert entry.ok is True

    row = (await db_session.execute(select(CloudLinkAudit))).scalar_one()
    assert (row.direction, row.kind, row.summary) == ("up", "status", "printer 3 is RUNNING")


async def test_an_audit_row_can_record_a_refusal(db_session: AsyncSession):
    from backend.app.services.cloud_link.store import write_audit

    entry = await write_audit(db_session, "down", "cmd", "pause rejected: printer offline", ok=False)
    assert entry.ok is False


async def test_pruning_deletes_only_what_is_older_than_the_window(db_session: AsyncSession):
    """The audit is the operator's only record of what the portal saw, so the
    sweep is bounded by time and by nothing else — never by a row count, which
    would throw away a busy day and keep a quiet month."""
    from backend.app.services.cloud_link.store import prune_audit

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db_session.add_all(
        [
            CloudLinkAudit(ts=now - timedelta(days=31), direction="up", kind="status", summary="ancient"),
            CloudLinkAudit(ts=now - timedelta(days=29), direction="up", kind="status", summary="recent"),
            CloudLinkAudit(ts=now, direction="up", kind="status", summary="fresh"),
        ]
    )
    await db_session.commit()

    deleted = await prune_audit(db_session)
    assert deleted == 1

    kept = sorted((await db_session.execute(select(CloudLinkAudit.summary))).scalars())
    assert kept == ["fresh", "recent"]


async def test_the_pruning_window_is_the_callers_to_widen(db_session: AsyncSession):
    from backend.app.services.cloud_link.store import prune_audit

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    db_session.add_all(
        [
            CloudLinkAudit(ts=now - timedelta(days=10), direction="up", kind="status", summary="ten"),
            CloudLinkAudit(ts=now - timedelta(days=2), direction="up", kind="status", summary="two"),
        ]
    )
    await db_session.commit()

    assert await prune_audit(db_session, older_than_days=7) == 1
    assert list((await db_session.execute(select(CloudLinkAudit.summary))).scalars()) == ["two"]


# ------------------------------------------------------------ the portal URL


def test_a_public_portal_must_be_reached_over_tls():
    """The link carries the instance secret and every command the portal
    sends. Plain http to a host on the internet publishes both."""
    from backend.app.services.cloud_link.store import validate_portal_url

    with pytest.raises(ValueError):
        validate_portal_url("http://example.com")
    with pytest.raises(ValueError):
        validate_portal_url("ws://example.com")


def test_the_two_tls_schemes_are_accepted():
    from backend.app.services.cloud_link.store import validate_portal_url

    assert validate_portal_url("https://cloud.bamdude.top") == "https://cloud.bamdude.top"
    assert validate_portal_url("wss://cloud.bamdude.top") == "wss://cloud.bamdude.top"


def test_a_portal_on_this_machine_needs_no_certificate():
    """A developer running the portal on localhost is not crossing a network,
    so demanding TLS there buys nothing and costs a self-signed certificate in
    every dev setup."""
    from backend.app.services.cloud_link.store import validate_portal_url

    assert validate_portal_url("http://localhost:3002") == "http://localhost:3002"
    assert validate_portal_url("http://127.0.0.1:3002") == "http://127.0.0.1:3002"


def test_a_url_without_a_scheme_or_a_host_is_not_a_portal():
    from backend.app.services.cloud_link.store import validate_portal_url

    for bad in ("", "   ", "cloud.bamdude.top", "//cloud.bamdude.top", "https://"):
        with pytest.raises(ValueError):
            validate_portal_url(bad)


def test_the_returned_url_is_normalised():
    """Trailing slash and surrounding whitespace are stripped, so the value
    stored is the one every caller concatenates a path onto."""
    from backend.app.services.cloud_link.store import validate_portal_url

    assert validate_portal_url("  https://cloud.bamdude.top/  ") == "https://cloud.bamdude.top"
    assert validate_portal_url("https://cloud.bamdude.top/portal/") == "https://cloud.bamdude.top/portal"
