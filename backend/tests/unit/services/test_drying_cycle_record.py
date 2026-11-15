"""What the MQTT client remembers of one AMS drying cycle (upstream 84f320b85).

The spools of a unit are marked as dried by what the printer SHOWED of the
cycle, not by what somebody asked for: the client keeps the highest countdown
seen, whether it watched the countdown start, and the target it sent itself.
When the countdown falls to zero that record is reported once.

Only a status frame says what the unit is doing — a command's acknowledgement
travels in the same envelope and must neither start nor end a cycle.
"""

from __future__ import annotations

import pytest

from backend.app.services.bambu_mqtt import DryingCycleEnd

CHECKING, DRYING = 1, 2


def _info(dry_status: int) -> str:
    return f"{dry_status << 4:x}"


@pytest.fixture
def ended():
    return []


@pytest.fixture
def client(ended):
    from backend.app.services.bambu_mqtt import BambuMQTTClient

    return BambuMQTTClient(
        ip_address="192.168.1.100",
        serial_number="TEST123",
        access_code="12345678",
        on_drying_cycle_end=ended.append,
    )


def _push(client, dry_time, *, dry_status: int | None = None, ams_id: int = 0, status: bool = True) -> None:
    unit: dict = {"id": str(ams_id), "tray": []}
    if dry_time is not None:
        unit["dry_time"] = dry_time
    if dry_status is not None:
        unit["info"] = _info(dry_status)
    client._handle_ams_data({"ams": [unit]}, presence_is_status=status)


class TestTheRecord:
    def test_a_cycle_watched_from_idle_reports_its_peak_and_last_minute(self, client, ended):
        _push(client, 0)
        _push(client, 480)
        _push(client, 300)
        _push(client, 120)
        _push(client, 0)

        assert ended == [DryingCycleEnd(ams_id=0, remaining_minutes=120, peak_minutes=480, start_seen=True)]

    def test_a_cycle_met_already_counting_is_not_seen_from_its_start(self, client, ended):
        _push(client, 200)
        _push(client, 3)
        _push(client, 0)

        assert ended == [DryingCycleEnd(ams_id=0, remaining_minutes=3, peak_minutes=200, start_seen=False)]

    def test_our_own_start_gives_the_target(self, client, ended):
        client._client = object.__new__(_Publisher)
        _push(client, 0)
        client.send_drying_command(ams_id=0, temp=55, duration=8, mode=1, filament="PLA")
        _push(client, 480)
        _push(client, 2)
        _push(client, 0)

        assert ended == [
            DryingCycleEnd(
                ams_id=0, remaining_minutes=2, peak_minutes=480, start_seen=True, target_temp=55, target_hours=8
            )
        ]

    def test_the_target_survives_our_own_stop(self, client, ended):
        """A stop drops the cached target before the countdown falls; the
        record copied it when the countdown began."""
        client._client = object.__new__(_Publisher)
        _push(client, 0)
        client.send_drying_command(ams_id=0, temp=65, duration=12, mode=1, filament="PETG")
        _push(client, 720)
        _push(client, 200)
        client.send_drying_command(ams_id=0, temp=0, duration=0, mode=0)
        _push(client, 0)

        assert len(ended) == 1
        assert (ended[0].target_temp, ended[0].target_hours, ended[0].remaining_minutes) == (65, 12, 200)

    def test_a_new_start_over_a_running_cycle_replaces_the_record(self, client, ended):
        client._client = object.__new__(_Publisher)
        _push(client, 0)
        client.send_drying_command(ams_id=0, temp=65, duration=12, mode=1, filament="PETG")
        _push(client, 720)
        _push(client, 700)
        client.send_drying_command(ams_id=0, temp=45, duration=2, mode=1, filament="PLA")
        _push(client, 120)
        _push(client, 1)
        _push(client, 0)

        assert ended == [
            DryingCycleEnd(
                ams_id=0, remaining_minutes=1, peak_minutes=120, start_seen=True, target_temp=45, target_hours=2
            )
        ]

    def test_a_cycle_started_at_the_printer_has_no_target(self, client, ended):
        _push(client, 0)
        _push(client, 240)
        _push(client, 0)

        assert (ended[0].target_temp, ended[0].target_hours) == (None, None)

    def test_units_are_kept_apart(self, client, ended):
        _push(client, 0, ams_id=0)
        _push(client, 0, ams_id=1)
        _push(client, 480, ams_id=0)
        _push(client, 60, ams_id=1)
        _push(client, 0, ams_id=1)

        assert ended == [DryingCycleEnd(ams_id=1, remaining_minutes=60, peak_minutes=60, start_seen=True)]

    def test_the_next_cycle_starts_a_record_of_its_own(self, client, ended):
        _push(client, 0)
        _push(client, 480)
        _push(client, 0)
        _push(client, 60)
        _push(client, 0)

        assert [(c.peak_minutes, c.start_seen) for c in ended] == [(480, True), (60, True)]


class TestWhatDoesNotEndACycle:
    def test_the_transient_zero_while_checking(self, client, ended):
        _push(client, 720, dry_status=CHECKING)
        _push(client, 0, dry_status=CHECKING)
        _push(client, 719, dry_status=DRYING)

        assert ended == []

        _push(client, 0, dry_status=0)
        assert ended == [DryingCycleEnd(ams_id=0, remaining_minutes=719, peak_minutes=720, start_seen=False)]

    def test_a_frame_without_dry_time(self, client, ended):
        _push(client, 60)
        _push(client, None)

        assert ended == []

    def test_an_idle_unit(self, client, ended):
        _push(client, 0)
        _push(client, 0)

        assert ended == []

    def test_a_command_acknowledgement_is_not_the_printers_status(self, client, ended):
        """An acknowledgement carrying AMS data neither ends the running cycle
        nor starts one."""
        _push(client, 0)
        _push(client, 480)
        _push(client, 0, status=False)
        assert ended == []

        _push(client, 100)
        _push(client, 0)
        assert ended == [DryingCycleEnd(ams_id=0, remaining_minutes=100, peak_minutes=480, start_seen=True)]

    def test_an_acknowledgement_does_not_start_a_cycle(self, client, ended):
        _push(client, 0)
        _push(client, 480, status=False)
        _push(client, 0)

        assert ended == []


class _Publisher:
    def publish(self, *_args, **_kwargs):
        return None
