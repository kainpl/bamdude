"""The AI failure notification names its detector: OctoEverywhere joins Obico.

OctoEverywhere has no confidence score; it reports print quality (1-10) instead. The
``ai_failure_detection`` template gains ``{provider}`` and ``{print_quality}`` (upstream Bambuddy
v1.3.0). The seed gives the stored template the new default body only where its body is exactly
one of the two shipped before (EN or UK): a body the operator changed stays as it is, and the name,
title and ``is_default`` are never touched. Run twice it changes nothing.

No DDL: the rows are named through a lightweight ``table``, never the ORM.
"""

import logging

logger = logging.getLogger(__name__)

version = 265
name = "ai_failure_template_provider"

# The bodies shipped before, frozen; the replacement of each is its own language's.
_AI_FAILURE_BODIES = (
    (
        "{printer}: {task_name}\nConfidence: {confidence}\nAction taken: {action}",
        "{printer}: {task_name}\nProvider: {provider}\nPrint quality: {print_quality}\n"
        "Confidence: {confidence}\nAction taken: {action}",
    ),
    (
        "{printer}: {task_name}\nВпевненість: {confidence}\nВиконана дія: {action}",
        "{printer}: {task_name}\nДетектор: {provider}\nЯкість друку: {print_quality}\n"
        "Впевненість: {confidence}\nВиконана дія: {action}",
    ),
)


async def upgrade(conn):
    """No DDL."""


async def seed(session_factory):
    from sqlalchemy import Text, column, table, update

    templates = table("notification_templates", column("event_type", Text), column("body_template", Text))

    async with session_factory() as db:
        for old_body, new_body in _AI_FAILURE_BODIES:
            await db.execute(
                update(templates)
                .where(templates.c.event_type == "ai_failure_detection", templates.c.body_template == old_body)
                .values(body_template=new_body)
            )
        await db.commit()
