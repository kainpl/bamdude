"""Per-chat printer scope for the Telegram bot (m159).

``NotificationProvider.printer_id`` was the last provider-level telegram
knob. It moved onto the chat as a LIST: the farm admin's chat watches every
printer while a partner's chat on the same bot watches only their machines —
notifications and bot control alike.
"""

from __future__ import annotations

from backend.app.models.telegram_chat import TelegramChat


def _chat(printer_ids):
    chat = TelegramChat(chat_id=1)
    chat.printer_ids = printer_ids
    return chat


class TestAllowsPrinter:
    def test_null_scope_watches_everything(self):
        assert _chat(None).allows_printer(7)
        assert _chat(None).allows_printer(None)

    def test_scoped_chat_sees_only_its_list(self):
        chat = _chat([3, 10])
        assert chat.allows_printer(3)
        assert chat.allows_printer(10)
        assert not chat.allows_printer(7)

    def test_single_printer_scope_works_too(self):
        chat = _chat([5])
        assert chat.allows_printer(5)
        assert not chat.allows_printer(6)

    def test_unattributed_events_pass_for_every_chat(self):
        """A test message or farm-wide news names no printer — filtering
        happens only where there is a printer to filter by."""
        assert _chat([3]).allows_printer(None)

    def test_empty_scope_sees_no_printer(self):
        chat = _chat([])
        assert not chat.allows_printer(1)
        assert chat.allows_printer(None)


class TestControlGuard:
    def test_no_chat_means_unscoped(self):
        from backend.app.services.telegram_handlers.common import chat_allows_printer

        assert chat_allows_printer(None, 42)

    def test_guard_follows_the_chat_scope(self):
        from backend.app.services.telegram_handlers.common import chat_allows_printer

        assert chat_allows_printer(_chat([2]), 2)
        assert not chat_allows_printer(_chat([2]), 3)


class TestProviderKnobRetired:
    def test_coercion_clears_the_provider_printer_binding(self):
        """m159: telegram's printer scope lives on each chat — the provider
        binding is forced clear like every other provider-level knob (m045)."""
        from backend.app.api.routes.notifications import _coerce_telegram_provider_fields
        from backend.app.models.notification import NotificationProvider

        provider = NotificationProvider(provider_type="telegram", name="t", config="{}")
        provider.printer_id = 5
        _coerce_telegram_provider_fields(provider)
        assert provider.printer_id is None

    def test_non_telegram_provider_keeps_its_binding(self):
        from backend.app.api.routes.notifications import _coerce_telegram_provider_fields
        from backend.app.models.notification import NotificationProvider

        provider = NotificationProvider(provider_type="ntfy", name="n", config="{}")
        provider.printer_id = 5
        _coerce_telegram_provider_fields(provider)
        assert provider.printer_id == 5

    def test_the_chat_scope_column_is_nullable(self):
        column = TelegramChat.__table__.c.printer_ids
        assert column.nullable
