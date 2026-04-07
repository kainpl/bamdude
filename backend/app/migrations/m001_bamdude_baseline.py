"""Baseline migration — FTS5 search index + seed data."""

from sqlalchemy import text

version = 1
name = "bamdude_baseline"


async def upgrade(conn):
    """Create FTS5 virtual table and triggers for archive full-text search."""
    await conn.execute(
        text("""
            CREATE VIRTUAL TABLE IF NOT EXISTS archive_fts USING fts5(
                print_name,
                filename,
                tags,
                notes,
                designer,
                filament_type,
                content='print_archives',
                content_rowid='id'
            )
        """)
    )

    await conn.execute(
        text("""
            CREATE TRIGGER IF NOT EXISTS archive_fts_insert AFTER INSERT ON print_archives BEGIN
                INSERT INTO archive_fts(rowid, print_name, filename, tags, notes, designer, filament_type)
                VALUES (new.id, new.print_name, new.filename, new.tags, new.notes, new.designer, new.filament_type);
            END
        """)
    )

    await conn.execute(
        text("""
            CREATE TRIGGER IF NOT EXISTS archive_fts_delete AFTER DELETE ON print_archives BEGIN
                INSERT INTO archive_fts(archive_fts, rowid, print_name, filename, tags, notes, designer, filament_type)
                VALUES ('delete', old.id, old.print_name, old.filename, old.tags, old.notes, old.designer, old.filament_type);
            END
        """)
    )

    await conn.execute(
        text("""
            CREATE TRIGGER IF NOT EXISTS archive_fts_update AFTER UPDATE ON print_archives BEGIN
                INSERT INTO archive_fts(archive_fts, rowid, print_name, filename, tags, notes, designer, filament_type)
                VALUES ('delete', old.id, old.print_name, old.filename, old.tags, old.notes, old.designer, old.filament_type);
                INSERT INTO archive_fts(rowid, print_name, filename, tags, notes, designer, filament_type)
                VALUES (new.id, new.print_name, new.filename, new.tags, new.notes, new.designer, new.filament_type);
            END
        """)
    )

    # Populate FTS from existing archives (for import scenario)
    try:
        await conn.execute(
            text(
                "INSERT OR IGNORE INTO archive_fts(rowid, print_name, filename, tags, notes, designer, filament_type) "
                "SELECT id, print_name, filename, tags, notes, designer, filament_type FROM print_archives"
            )
        )
    except Exception:
        pass  # Table may be empty on fresh install


# ---------------------------------------------------------------------------
# Seed constants
# ---------------------------------------------------------------------------

# Map old permissions to new ones for migration
# Administrators get *_all permissions, Operators get *_own permissions
PERMISSION_MIGRATION_ALL = {
    "queue:update": "queue:update_all",
    "queue:delete": "queue:delete_all",
    "archives:update": "archives:update_all",
    "archives:delete": "archives:delete_all",
    "archives:reprint": "archives:reprint_all",
    "library:update": "library:update_all",
    "library:delete": "library:delete_all",
}

PERMISSION_MIGRATION_OWN = {
    "queue:update": "queue:update_own",
    "queue:delete": "queue:delete_own",
    "archives:update": "archives:update_own",
    "archives:delete": "archives:delete_own",
    "archives:reprint": "archives:reprint_own",
    "library:update": "library:update_own",
    "library:delete": "library:delete_own",
}

DEFAULT_MACROS = [
    {
        "name": "Swap Mode. Start Sequence",
        "printer_models": '["A1 Mini"]',
        "swap_mode_only": True,
        "event": "swap_mode_start",
        "gcode": (
            ";swap ini code\n"
            "G91 ;\n"
            "G0 Z50 F1000;\n"
            "G0 Z-20;\n"
            "G90;\n"
            "G28 XY;\n"
            "G0 Y-4 F5000; grab\n"
            "G0 Y145; pull and fix the plate\n"
            "G0 Y115 F1000; rehook\n"
            "G0 Y180 F5000; pull\n"
            "G4 P500; wait\n"
            "G0 Y186.5 F200; fix the plate\n"
            "G4 P500; wait\n"
            "G0 Y3 F15000; back\n"
            "G0 Y-5 F200; snap\n"
            "G4 P500; wait\n"
            "G0 Y10 F1000; load\n"
            "G0 Y20 F15000; ready\n"
        ),
    },
    {
        "name": "Swap Mode. Change Table",
        "printer_models": '["A1 Mini"]',
        "swap_mode_only": True,
        "event": "swap_mode_change_table",
        "gcode": (
            ";swap\n"
            "G0 X-10 F5000;\n"
            "G0 Z175;\n"
            "G0 Y-5 F2000;\n"
            "G0 Y186.5 F2000;\n"
            "G0 Y182 F10000;\n"
            "G0 Z186;\n"
            "G0 Y120 F500;\n"
            "G0 Y-4 Z175 F5000;\n"
            "G0 Y145;\n"
            "G0 Y115 F1000;\n"
            "G0 Y25 F500;\n"
            "G0 Y85 F1000;\n"
            "G0 Y180 F2000;\n"
            "G4 P500; wait\n"
            "G0 Y186.5 F200;\n"
            "G4 P500; wait\n"
            "G0 Y3 F3000;\n"
            "G0 Y-5 F200;\n"
            "G4 P500; wait\n"
            "G0 Y10 F1000;\n"
            "G0 Z100 Y186 F2000;\n"
            "G0 Y150;\n"
            "G4 P1000; wait\n"
        ),
    },
]


# ---------------------------------------------------------------------------
# Seed functions
# ---------------------------------------------------------------------------


async def _seed_notification_templates(session_factory):
    """Seed default notification templates if they don't exist."""
    from sqlalchemy import select

    from backend.app.models.notification_template import DEFAULT_TEMPLATES, NotificationTemplate

    async with session_factory() as session:
        # Get existing template event types
        result = await session.execute(select(NotificationTemplate.event_type))
        existing_types = {row[0] for row in result.fetchall()}

        if not existing_types:
            # No templates exist - insert all defaults
            for template_data in DEFAULT_TEMPLATES:
                template = NotificationTemplate(
                    event_type=template_data["event_type"],
                    name=template_data["name"],
                    title_template=template_data["title_template"],
                    body_template=template_data["body_template"],
                    is_default=True,
                )
                session.add(template)
        else:
            # Templates exist - only add missing ones
            for template_data in DEFAULT_TEMPLATES:
                if template_data["event_type"] not in existing_types:
                    template = NotificationTemplate(
                        event_type=template_data["event_type"],
                        name=template_data["name"],
                        title_template=template_data["title_template"],
                        body_template=template_data["body_template"],
                        is_default=True,
                    )
                    session.add(template)

        await session.commit()


async def _seed_default_groups(session_factory):
    """Seed default groups and migrate existing users to appropriate groups.

    Creates the default system groups (Administrators, Operators, Viewers) if they
    don't exist, then migrates existing users:
    - Users with role='admin' -> Administrators group
    - Users with role='user' -> Operators group

    Also migrates old permissions to new ownership-based permissions (Issue #205).
    """
    import logging

    from sqlalchemy import select

    from backend.app.core.permissions import DEFAULT_GROUPS
    from backend.app.models.group import Group
    from backend.app.models.user import User

    logger = logging.getLogger(__name__)

    async with session_factory() as session:
        # Get existing groups
        result = await session.execute(select(Group))
        existing_groups = {group.name: group for group in result.scalars().all()}

        # Create default groups if they don't exist
        groups_created = []
        for group_name, group_config in DEFAULT_GROUPS.items():
            if group_name not in existing_groups:
                group = Group(
                    name=group_name,
                    description=group_config["description"],
                    permissions=group_config["permissions"],
                    is_system=group_config["is_system"],
                )
                session.add(group)
                groups_created.append(group_name)
                logger.info("Created default group: %s", group_name)
            else:
                # Migrate existing group's permissions from old to new format
                group = existing_groups[group_name]
                if group.permissions:
                    updated = False
                    new_permissions = list(group.permissions)

                    # Determine which migration map to use based on group
                    migration_map = (
                        PERMISSION_MIGRATION_ALL if group_name == "Administrators" else PERMISSION_MIGRATION_OWN
                    )

                    for old_perm, new_perm in migration_map.items():
                        if old_perm in new_permissions:
                            new_permissions.remove(old_perm)
                            if new_perm not in new_permissions:
                                new_permissions.append(new_perm)
                            updated = True
                            logger.info(
                                "Migrated permission '%s' to '%s' in group '%s'", old_perm, new_perm, group_name
                            )

                    # For Administrators, also ensure they get *_all permissions if they have any new *_own
                    if group_name == "Administrators":
                        for _own_perm, all_perm in [
                            ("queue:update_own", "queue:update_all"),
                            ("queue:delete_own", "queue:delete_all"),
                            ("archives:update_own", "archives:update_all"),
                            ("archives:delete_own", "archives:delete_all"),
                            ("archives:reprint_own", "archives:reprint_all"),
                            ("library:update_own", "library:update_all"),
                            ("library:delete_own", "library:delete_all"),
                        ]:
                            # Add *_all if not present
                            if all_perm not in new_permissions:
                                new_permissions.append(all_perm)
                                updated = True

                    if updated:
                        group.permissions = new_permissions

        await session.commit()

        # Migrate new permissions: grant printers:clear_plate to all groups with printers:control
        result = await session.execute(select(Group))
        all_groups = result.scalars().all()
        for group in all_groups:
            if (
                group.permissions
                and "printers:control" in group.permissions
                and "printers:clear_plate" not in group.permissions
            ):
                group.permissions = [*group.permissions, "printers:clear_plate"]
                logger.info("Added printers:clear_plate to group '%s' (has printers:control)", group.name)
        await session.commit()

        # Migrate existing users to groups if they're not already in any group
        if groups_created:
            # Refresh to get newly created groups
            admin_result = await session.execute(select(Group).where(Group.name == "Administrators"))
            admin_group = admin_result.scalar_one_or_none()

            operators_result = await session.execute(select(Group).where(Group.name == "Operators"))
            operators_group = operators_result.scalar_one_or_none()

            # Get all users
            users_result = await session.execute(select(User))
            users = users_result.scalars().all()

            for user in users:
                # Skip if user already has groups
                if user.groups:
                    continue

                if user.role == "admin" and admin_group:
                    user.groups.append(admin_group)
                    logger.info("Migrated admin user '%s' to Administrators group", user.username)
                elif operators_group:
                    user.groups.append(operators_group)
                    logger.info("Migrated user '%s' to Operators group", user.username)

            await session.commit()


async def _seed_spool_catalog(session_factory):
    """Seed the spool catalog with default entries if empty."""
    import logging

    from sqlalchemy import func, select

    from backend.app.core.catalog_defaults import DEFAULT_SPOOL_CATALOG
    from backend.app.models.spool_catalog import SpoolCatalogEntry

    logger = logging.getLogger(__name__)

    async with session_factory() as session:
        result = await session.execute(select(func.count()).select_from(SpoolCatalogEntry))
        count = result.scalar() or 0
        if count > 0:
            return  # Already seeded

        for name, weight in DEFAULT_SPOOL_CATALOG:
            session.add(SpoolCatalogEntry(name=name, weight=weight, is_default=True))
        await session.commit()
        logger.info("Seeded %d default spool catalog entries", len(DEFAULT_SPOOL_CATALOG))


async def _seed_color_catalog(session_factory):
    """Seed the color catalog with default entries if empty."""
    import logging

    from sqlalchemy import func, select

    from backend.app.core.catalog_defaults import DEFAULT_COLOR_CATALOG
    from backend.app.models.color_catalog import ColorCatalogEntry

    logger = logging.getLogger(__name__)

    async with session_factory() as session:
        result = await session.execute(select(func.count()).select_from(ColorCatalogEntry))
        count = result.scalar() or 0
        if count > 0:
            return  # Already seeded

        for manufacturer, color_name, hex_color, material in DEFAULT_COLOR_CATALOG:
            session.add(
                ColorCatalogEntry(
                    manufacturer=manufacturer,
                    color_name=color_name,
                    hex_color=hex_color,
                    material=material,
                    is_default=True,
                )
            )
        await session.commit()
        logger.info("Seeded %d default color catalog entries", len(DEFAULT_COLOR_CATALOG))


async def _seed_default_macros(session_factory):
    """Seed built-in macros if they don't exist yet."""
    import logging

    from sqlalchemy import select

    from backend.app.models.macro import Macro

    logger = logging.getLogger(__name__)

    async with session_factory() as session:
        # Check if we already have built-in macros
        result = await session.execute(select(Macro).where(Macro.is_custom == False))  # noqa: E712
        existing = result.scalars().all()

        added = 0
        updated = 0
        existing_by_event = {m.event: m for m in existing}

        for macro_def in DEFAULT_MACROS:
            key = macro_def["event"]
            if key not in existing_by_event:
                session.add(Macro(**macro_def, is_custom=False))
                added += 1
            else:
                # Update gcode if currently empty (first run after adding default gcode)
                macro = existing_by_event[key]
                if not macro.gcode and macro_def.get("gcode"):
                    macro.gcode = macro_def["gcode"]
                    updated += 1

        if added or updated:
            await session.commit()
            if added:
                logger.info("Seeded %d default macros", added)
            if updated:
                logger.info("Updated gcode for %d default macros", updated)


async def seed(session_factory):
    """Run all seed functions."""
    await _seed_notification_templates(session_factory)
    await _seed_default_groups(session_factory)
    await _seed_maintenance_types(session_factory)
    await _seed_spool_catalog(session_factory)
    await _seed_color_catalog(session_factory)
    await _seed_default_macros(session_factory)


async def _seed_maintenance_types(session_factory):
    """Seed default maintenance types if they don't exist."""
    from backend.app.api.routes.maintenance import ensure_default_types

    async with session_factory() as session:
        await ensure_default_types(session)
