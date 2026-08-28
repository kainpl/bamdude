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


class TestTheSettingExists:
    def test_default_is_zero_meaning_always_send(self):
        from backend.app.schemas.settings import AppSettings

        assert AppSettings().notify_progress_min_duration_minutes == 0

    def test_the_settings_route_coerces_it_to_int(self):
        """The key must sit in the route's int-coercion list, or the frontend
        receives a string and the number input silently misbehaves."""
        import inspect

        from backend.app.api.routes import settings as settings_route

        source = inspect.getsource(settings_route)
        assert '"notify_progress_min_duration_minutes"' in source
