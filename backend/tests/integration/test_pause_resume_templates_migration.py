"""Integration tests for m052_notification_pause_resume_events.seed().

Covers the locale-aware seeding contract:

* UK install → seed inserts UK templates from
  ``data/notification_templates_uk.json``.
* No language setting → English defaults from ``DEFAULT_TEMPLATES``.
* Unsupported language → English fallback (defence in depth).
* Existing rows are preserved (idempotent re-run).

The migration's ``upgrade()`` (column-add on ``notification_providers``) is
trivial DDL that's already covered by the project's broader migration
smoke-tests; this file zeroes in on the seed path because that's where the
locale-aware behaviour lives and where regressions hurt operators most
(silent English templates on a UK install).
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.migrations import m052_notification_pause_resume_events
from backend.app.models.notification_template import NotificationTemplate
from backend.app.models.settings import Settings


@pytest_asyncio.fixture
async def session_factory(test_engine):
    """Wrap the project-wide test engine in an ``async_sessionmaker`` so the
    migration's ``seed(session_factory)`` signature is satisfied."""
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    yield factory


async def _fetch_template(db, event_type: str) -> NotificationTemplate | None:
    result = await db.execute(select(NotificationTemplate).where(NotificationTemplate.event_type == event_type))
    return result.scalar_one_or_none()


@pytest.mark.asyncio
async def test_seed_inserts_ukrainian_templates_when_language_is_uk(session_factory):
    """UK-configured install gets UK template copy, not English defaults."""
    async with session_factory() as db:
        db.add(Settings(key="language", value="uk"))
        await db.commit()

    await m052_notification_pause_resume_events.seed(session_factory)

    async with session_factory() as db:
        paused = await _fetch_template(db, "print_paused")
        resumed = await _fetch_template(db, "print_resumed")

    assert paused is not None, "print_paused row must be inserted"
    assert resumed is not None, "print_resumed row must be inserted"
    # Ukrainian copy from data/notification_templates_uk.json.
    assert paused.name == "Друк на паузі"
    assert paused.title_template == "Друк на паузі"
    assert "Причина" in paused.body_template
    assert resumed.name == "Друк відновлено"
    assert "Тривалість паузи" in resumed.body_template
    # is_default must be True so locale_updater later UPDATEs it on language switch.
    assert paused.is_default is True
    assert resumed.is_default is True


@pytest.mark.asyncio
async def test_seed_falls_back_to_english_when_no_language_setting(session_factory):
    """Missing language setting → English defaults from DEFAULT_TEMPLATES."""
    # No Settings row inserted at all.
    await m052_notification_pause_resume_events.seed(session_factory)

    async with session_factory() as db:
        paused = await _fetch_template(db, "print_paused")
        resumed = await _fetch_template(db, "print_resumed")

    assert paused is not None
    assert resumed is not None
    # English defaults — DEFAULT_TEMPLATES owns the canonical copy.
    assert paused.name == "Print Paused"
    assert paused.body_template.startswith("{printer}: {filename}")
    assert "Reason" in paused.body_template
    assert resumed.name == "Print Resumed"
    assert "Paused for" in resumed.body_template


@pytest.mark.asyncio
async def test_seed_falls_back_to_english_for_unsupported_language(session_factory):
    """Language not in _SUPPORTED_LOCALES → English fallback (defence in depth)."""
    async with session_factory() as db:
        db.add(Settings(key="language", value="de"))  # not in {"en", "uk"}
        await db.commit()

    await m052_notification_pause_resume_events.seed(session_factory)

    async with session_factory() as db:
        paused = await _fetch_template(db, "print_paused")

    assert paused is not None
    # English copy because 'de' isn't supported.
    assert paused.name == "Print Paused"


@pytest.mark.asyncio
async def test_seed_is_idempotent_existing_rows_preserved(session_factory):
    """Pre-existing rows for the new event_types must survive a re-run.

    Operator may have already customised the template name / body — the
    seed must not overwrite those edits on subsequent runs (e.g. dev mode
    re-running the latest migration on every startup).
    """
    async with session_factory() as db:
        db.add(
            NotificationTemplate(
                event_type="print_paused",
                name="My Custom Pause Name",
                title_template="CUSTOM: paused",
                body_template="custom body",
                is_default=False,  # operator marked this non-default after edit
            )
        )
        db.add(Settings(key="language", value="uk"))
        await db.commit()

    await m052_notification_pause_resume_events.seed(session_factory)

    async with session_factory() as db:
        paused = await _fetch_template(db, "print_paused")
        resumed = await _fetch_template(db, "print_resumed")

    # Custom row preserved as-is.
    assert paused.name == "My Custom Pause Name"
    assert paused.title_template == "CUSTOM: paused"
    assert paused.is_default is False
    # Missing row was inserted (resumed didn't exist before).
    assert resumed is not None
    assert resumed.name == "Друк відновлено"


@pytest.mark.asyncio
async def test_seed_rerun_does_not_duplicate_rows(session_factory):
    """Calling seed() twice must leave exactly one row per event_type."""
    async with session_factory() as db:
        db.add(Settings(key="language", value="en"))
        await db.commit()

    await m052_notification_pause_resume_events.seed(session_factory)
    await m052_notification_pause_resume_events.seed(session_factory)

    async with session_factory() as db:
        paused = await db.execute(select(NotificationTemplate).where(NotificationTemplate.event_type == "print_paused"))
        resumed = await db.execute(
            select(NotificationTemplate).where(NotificationTemplate.event_type == "print_resumed")
        )
        paused_rows = list(paused.scalars().all())
        resumed_rows = list(resumed.scalars().all())

    assert len(paused_rows) == 1, "Re-running seed must not duplicate print_paused row"
    assert len(resumed_rows) == 1, "Re-running seed must not duplicate print_resumed row"
