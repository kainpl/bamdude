"""One retention setting, two kinds of file.

``log_retention_days`` used to mean the rotated application log and nothing
else, so MQTT recordings — which are written into the same log folder — grew
until somebody noticed. They cannot join the rotating handler: the recorder
writes from its own thread, deliberately outside ``logging``, so it never blocks
paho's network thread. What they can share is the moment the operator's answer
is applied, which is this function.

⚠️ The wiring is the point. ``prune`` being correct proves nothing if nothing
calls it, and the failure would be invisible — a folder quietly filling up.
"""

from unittest.mock import MagicMock, patch

import pytest

from backend.app.core.logging_state import update_log_retention

MOD = "backend.app.services.mqtt_recorder"

pytestmark = pytest.mark.unit


def test_applying_retention_also_expires_recordings():
    with patch(f"{MOD}.mqtt_recorder.prune", MagicMock(return_value=0)) as prune:
        update_log_retention(14)

    prune.assert_called_once_with(14)


def test_the_floor_the_handler_uses_is_the_one_recordings_get():
    """⚠️ Same clamp, or a 0 in the database would mean "keep one day of log"
    and "delete every recording" at once."""
    with patch(f"{MOD}.mqtt_recorder.prune", MagicMock(return_value=0)) as prune:
        update_log_retention(0)

    prune.assert_called_once_with(1)


def test_a_recording_sweep_that_fails_does_not_stop_the_log_setting():
    """Best-effort: tidying old captures must never be able to leave the
    application log without its retention."""
    handler = MagicMock()
    with (
        patch("backend.app.core.logging_state._handler", handler),
        patch(f"{MOD}.mqtt_recorder.prune", MagicMock(side_effect=OSError("disk gone"))),
    ):
        update_log_retention(9)

    assert handler.backupCount == 9


def test_it_still_works_before_the_handler_exists():
    """⚠️ The early-return this replaced skipped the recordings too. Retention
    can be applied from a process that never built a file handler."""
    with (
        patch("backend.app.core.logging_state._handler", None),
        patch(f"{MOD}.mqtt_recorder.prune", MagicMock(return_value=0)) as prune,
    ):
        update_log_retention(5)

    prune.assert_called_once_with(5)
