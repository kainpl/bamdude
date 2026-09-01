"""Update DB templates and system data when system language changes."""

import json
import logging
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"

SUPPORTED_LOCALES = {"en", "uk"}


async def update_locale_data(db: AsyncSession, language: str) -> dict:
    """Update notification templates and maintenance types for the given language.

    Only updates records marked as default/system - user-customized ones are not touched.
    Returns counts of updated records.
    """
    lang = language if language in SUPPORTED_LOCALES else "en"

    notification_count = await _update_notification_templates(db, lang)
    maintenance_count = await _update_maintenance_types(db, lang)

    await db.commit()

    # Invalidate cached locale-to-english name mapping
    try:
        import backend.app.api.routes.maintenance as maint_module

        maint_module._locale_name_to_english = None
    except Exception:
        pass

    # Invalidate i18n translation cache, and stamp the new language for the
    # sync readers (see i18n.current_language). Stamped from ``lang`` rather
    # than re-read, so the cache cannot disagree with the write that caused it.
    try:
        from backend.app.i18n import invalidate_cache, set_language_cache

        invalidate_cache()
        set_language_cache(lang)
    except Exception:
        pass

    logger.info(
        "Locale data updated to '%s': %d notification templates, %d maintenance types",
        lang,
        notification_count,
        maintenance_count,
    )

    return {
        "language": lang,
        "notification_templates_updated": notification_count,
        "maintenance_types_updated": maintenance_count,
    }


async def _update_notification_templates(db: AsyncSession, lang: str) -> int:
    """Bring the DB's default notification templates in line with the shipped JSON.

    ⚠️ **Creates as well as updates.** This used to issue an UPDATE only, so a
    template added to the JSON after the DB was first seeded never appeared:
    ``rowcount`` came back 0 and the loop moved on. Four shipped templates were
    dead that way — both filament-runout ones and the two stock alerts — and a
    farm lost every runout notification in silence, because the count this
    returns is rows TOUCHED and a missing template looked exactly like an
    unchanged one. A new event type is now a JSON entry and nothing else; it
    lands on the next startup without a migration.

    ⚠️ ``event_type`` is UNIQUE, and a template the operator has edited keeps
    the same single row with ``is_default=False``. So an insert happens only
    when no row exists AT ALL — otherwise the sync would either break the
    constraint or overwrite the very customisation it exists to preserve.
    """
    from backend.app.models.notification_template import NotificationTemplate

    file_path = DATA_DIR / f"notification_templates_{lang}.json"
    if not file_path.exists():
        logger.warning("Notification templates file not found: %s", file_path)
        return 0

    with open(file_path, encoding="utf-8") as f:
        templates = json.load(f)

    updated = 0
    created: list[str] = []
    for event_type, data in templates.items():
        result = await db.execute(
            update(NotificationTemplate)
            .where(
                NotificationTemplate.event_type == event_type,
                NotificationTemplate.is_default == True,  # noqa: E712
            )
            .values(
                name=data["name"],
                title_template=data["title_template"],
                body_template=data["body_template"],
            )
        )
        if result.rowcount:
            updated += result.rowcount
            continue
        # Nothing was updated: the row is either absent, or present and
        # customised. Only the first case is ours to fill in.
        present = await db.scalar(select(NotificationTemplate.id).where(NotificationTemplate.event_type == event_type))
        if present is not None:
            continue
        db.add(
            NotificationTemplate(
                event_type=event_type,
                name=data["name"],
                title_template=data["title_template"],
                body_template=data["body_template"],
                is_default=True,
            )
        )
        await db.flush()
        created.append(event_type)

    if created:
        logger.info(
            "Notification templates created from the %s locale (they had no row): %s",
            lang,
            ", ".join(sorted(created)),
        )
    return updated + len(created)


async def _update_maintenance_types(db: AsyncSession, lang: str) -> int:
    """Update system maintenance types from locale JSON."""
    from backend.app.models.maintenance import MaintenanceType

    file_path = DATA_DIR / f"maintenance_types_{lang}.json"
    if not file_path.exists():
        logger.warning("Maintenance types file not found: %s", file_path)
        return 0

    with open(file_path, encoding="utf-8") as f:
        types = json.load(f)

    # Maintenance types are matched by their original English name (stored as key)
    # We need to find by is_system=True and match by current name OR original key
    result = await db.execute(
        select(MaintenanceType).where(MaintenanceType.is_system == True)  # noqa: E712
    )
    system_types = list(result.scalars().all())

    # Build reverse lookup: any known name (from any locale) -> original key
    all_locale_names = {}
    for locale in SUPPORTED_LOCALES:
        lf = DATA_DIR / f"maintenance_types_{locale}.json"
        if lf.exists():
            with open(lf, encoding="utf-8") as f:
                locale_data = json.load(f)
                for key, val in locale_data.items():
                    all_locale_names[val["name"]] = key

    count = 0
    for mt in system_types:
        # Find original key by current name
        original_key = all_locale_names.get(mt.name, mt.name)

        if original_key in types:
            data = types[original_key]
            mt.name = data["name"]
            mt.description = data["description"]
            count += 1

    return count
