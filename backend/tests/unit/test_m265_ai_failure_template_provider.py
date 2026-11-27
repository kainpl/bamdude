"""m265: the AI failure template names its detector, where the operator did not change it."""

import pytest
from sqlalchemy import select

from backend.app.migrations import m265_ai_failure_template_provider as m265
from backend.app.models.notification_template import NotificationTemplate

pytestmark = pytest.mark.asyncio


def _factory(db_session):
    class _Session:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *exc):
            return False

    return _Session


@pytest.mark.parametrize(("old", "new"), m265._AI_FAILURE_BODIES)
async def test_a_shipped_body_gets_the_new_default(db_session, old, new):
    db_session.add(
        NotificationTemplate(event_type="ai_failure_detection", name="AI", title_template="t", body_template=old)
    )
    await db_session.commit()
    await m265.seed(_factory(db_session))
    await m265.seed(_factory(db_session))  # run twice changes nothing
    body = await db_session.scalar(
        select(NotificationTemplate.body_template).where(NotificationTemplate.event_type == "ai_failure_detection")
    )
    assert body == new
    assert "{provider}" in new and "{print_quality}" in new


async def test_an_edited_body_stays(db_session):
    db_session.add(
        NotificationTemplate(event_type="ai_failure_detection", name="AI", title_template="t", body_template="mine")
    )
    await db_session.commit()
    await m265.seed(_factory(db_session))
    body = await db_session.scalar(
        select(NotificationTemplate.body_template).where(NotificationTemplate.event_type == "ai_failure_detection")
    )
    assert body == "mine"
