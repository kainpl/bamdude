"""Every notification event is reachable from every settings surface.

The 2026-08-28 sweep found three holes this file now pins shut:
``ams_drying_suspended`` and ``filament_deficit`` were in the chat
vocabulary (and its DEFAULTS) but missing from the route's category list —
so the chat dialog could not display, let alone untick them; and
``filament_runout`` was missing from the vocabulary entirely while the
sender used ``filament_runout_backup`` as an event type, which made
telegram chats silently unreachable for runouts.
"""

from __future__ import annotations

import inspect

from backend.app.api.routes.telegram import EVENT_CATEGORIES, EVENT_LABELS
from backend.app.models.notification import PROVIDER_EVENT_DEFAULTS
from backend.app.models.telegram_chat import ALL_NOTIFY_EVENTS, DEFAULT_NOTIFY_EVENTS


class TestProviderRegistryMatchesTheSchemas:
    def test_create_schema_covers_the_registry_exactly(self):
        from backend.app.schemas.notification import NotificationProviderCreate

        fields = {f for f in NotificationProviderCreate.model_fields if f.startswith("on_")}
        assert fields == set(PROVIDER_EVENT_DEFAULTS)

    def test_update_schema_covers_the_registry_exactly(self):
        from backend.app.schemas.notification import NotificationProviderUpdate

        fields = {f for f in NotificationProviderUpdate.model_fields if f.startswith("on_")}
        assert fields == set(PROVIDER_EVENT_DEFAULTS)

    def test_schema_defaults_mirror_the_registry(self):
        from backend.app.schemas.notification import NotificationProviderCreate

        for field, default in PROVIDER_EVENT_DEFAULTS.items():
            assert NotificationProviderCreate.model_fields[field].default == default, field


class TestChatVocabularyMatchesTheDialog:
    def test_every_vocabulary_event_sits_in_a_category(self):
        """The chat dialog renders exactly the category lists — an event
        outside them is un-untickable, however loudly the vocabulary and the
        defaults claim it exists."""
        cat_events = {e for c in EVENT_CATEGORIES.values() for e in c["events"]}
        assert cat_events == set(ALL_NOTIFY_EVENTS)

    def test_every_vocabulary_event_has_a_label(self):
        assert set(EVENT_LABELS) == set(ALL_NOTIFY_EVENTS)

    def test_defaults_are_a_subset_of_the_vocabulary(self):
        assert set(DEFAULT_NOTIFY_EVENTS) <= set(ALL_NOTIFY_EVENTS)


class TestRunoutReachesTelegram:
    def test_filament_runout_is_in_the_chat_vocabulary(self):
        assert "filament_runout" in ALL_NOTIFY_EVENTS

    def test_the_sender_normalises_both_flavours_to_one_event_type(self):
        """The backup-switch flavour picks a different TEMPLATE but must send
        the same chat-facing event type — ``filament_runout_backup`` is not
        in any vocabulary and would (did) vanish silently."""
        from backend.app.services.notification_service import NotificationService

        source = inspect.getsource(NotificationService.on_filament_runout)
        assert '"filament_runout_backup"' in source  # the template choice stays
        assert 'db, "filament_runout", printer_id' in source  # the event type is fixed
