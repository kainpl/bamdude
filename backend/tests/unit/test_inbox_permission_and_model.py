"""notifications:inbox is a group permission of every default group and never an API-key one."""

import pytest

from backend.app.core.auth import _APIKEY_DENIED_PERMISSIONS, _APIKEY_SCOPE_BY_PERMISSION
from backend.app.core.permissions import ALL_PERMISSIONS, DEFAULT_GROUPS, PERMISSION_CATEGORIES, Permission
from backend.app.models.user import User


def test_the_permission_exists_and_is_catalogued():
    assert Permission.NOTIFICATIONS_INBOX == "notifications:inbox"
    assert "notifications:inbox" in ALL_PERMISSIONS
    assert Permission.NOTIFICATIONS_INBOX in PERMISSION_CATEGORIES["Notifications"]


@pytest.mark.parametrize("group", ["Administrators", "Operators", "Viewers"])
def test_every_default_group_has_an_inbox(group):
    assert "notifications:inbox" in DEFAULT_GROUPS[group]["permissions"]


def test_api_keys_never_get_an_inbox():
    assert Permission.NOTIFICATIONS_INBOX in _APIKEY_DENIED_PERMISSIONS
    assert Permission.NOTIFICATIONS_INBOX not in _APIKEY_SCOPE_BY_PERMISSION


def test_user_wants_inbox_event_reads_is_active_and_the_list():
    user = User(username="u", is_active=True, inbox_events=None)
    assert user.wants_inbox_event("print_failed") is True
    assert user.wants_inbox_event("print_complete") is False
    user.inbox_events = ["print_complete"]
    assert user.wants_inbox_event("print_complete") is True
    user.is_active = False
    assert user.wants_inbox_event("print_complete") is False
