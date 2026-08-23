"""Two-push debounce for downward AMS weight corrections.

The AMS sync was deliberately increase-only after the X2D incident: one bogus
push two seconds after an MQTT reconnect wrote three 1 kg spools off in 16 ms.
Zeros are refused before this module is ever consulted
(``inv-zero-remain-is-not-an-empty-spool``); the debounce covers the rest of
the reconnect-burst risk for non-zero values — a decrease is applied only when
the same reading arrives twice, at least a window apart, from the same
physical spool (``tray_uuid``).

Pure logic, shared as a module singleton by both call sites (internal
inventory + Spoolman) so a candidate confirmed by one is not re-armed by the
other.
"""

import time


class DecreaseDebounce:
    def __init__(self, window_seconds: float = 60.0):
        self.window_seconds = window_seconds
        # (printer_id, ams_id, tray_id) → (value, tray_uuid, first_seen_monotonic)
        self._candidates: dict[tuple[int, int, int], tuple[float, str, float]] = {}

    def offer(self, key: tuple[int, int, int], value: float, tray_uuid: str, now: float | None = None) -> bool:
        """Offer a downward-correction candidate; True when it is confirmed.

        Confirmation requires a PREVIOUS offer of the same value from the same
        ``tray_uuid`` at least ``window_seconds`` ago. A different value or a
        different uuid replaces the candidate and restarts the clock; a
        confirmed offer clears it (the next decrease starts fresh).
        """
        if now is None:
            now = time.monotonic()
        candidate = self._candidates.get(key)
        if candidate is not None:
            prev_value, prev_uuid, first_seen = candidate
            if prev_value == value and prev_uuid == tray_uuid:
                if now - first_seen >= self.window_seconds:
                    del self._candidates[key]
                    return True
                return False
        self._candidates[key] = (value, tray_uuid, now)
        return False

    def clear(self, key: tuple[int, int, int]) -> None:
        self._candidates.pop(key, None)


# Shared by main.on_ams_change's internal and Spoolman legs.
debounce = DecreaseDebounce()
