"""Progress milestones only for prints longer than the configured floor (#28).

A 30-minute job used to collect six notifications (plate, first layer,
25/50/75%, done). The floor mutes just the three middle ones, decided from a
stateless estimate at each crossing: ``remaining / (share still to print)``.
"""

from __future__ import annotations

from backend.app.main import _estimated_total_print_minutes


class TestTheEstimate:
    def test_quarter_way_with_90_left_is_a_two_hour_job(self):
        assert _estimated_total_print_minutes(25, 90) == 120.0

    def test_half_way_with_15_left_is_a_half_hour_job(self):
        assert _estimated_total_print_minutes(50, 15) == 30.0

    def test_three_quarters_with_360_left_is_a_daylong_job(self):
        assert _estimated_total_print_minutes(75, 360) == 1440.0

    def test_unknown_remaining_fails_open(self):
        assert _estimated_total_print_minutes(25, None) is None
        assert _estimated_total_print_minutes(25, 0) is None

    def test_edge_progress_fails_open(self):
        """0% would divide by nothing meaningful, 100% by zero — both mean
        the crossing math has no honest answer, so no gating happens."""
        assert _estimated_total_print_minutes(0, 90) is None
        assert _estimated_total_print_minutes(100, 90) is None

    def test_negative_remaining_fails_open(self):
        assert _estimated_total_print_minutes(25, -5) is None


class TestTheFloorDecision:
    """The per-recipient gate: ``NotificationService._passes_progress_floor``."""

    def _passes(self, floor, est):
        from backend.app.services.notification_service import NotificationService

        return NotificationService._passes_progress_floor(floor, est)

    def test_zero_floor_always_sends(self):
        assert self._passes(0, 5.0)

    def test_unknown_estimate_fails_open(self):
        assert self._passes(60, None)

    def test_short_print_is_muted_long_print_is_not(self):
        assert not self._passes(60, 30.0)
        assert self._passes(60, 60.0)
        assert self._passes(60, 240.0)


class TestPerChatOverride:
    def test_the_chat_carries_its_own_floor_nullable(self):
        """m157: each chat carries its own floor (NULL or 0 = always send) —
        an admin's 60-minute floor must not decide for an operator's chat
        that wants 10."""
        from backend.app.models.telegram_chat import TelegramChat

        column = TelegramChat.__table__.c.progress_min_duration_minutes
        assert column.nullable

    def test_the_provider_carries_its_own_floor_nullable(self):
        """m158: every non-telegram provider carries its own floor too — a
        phone push and an email digest legitimately want different floors."""
        from backend.app.models.notification import NotificationProvider

        column = NotificationProvider.__table__.c.progress_min_duration_minutes
        assert column.nullable
