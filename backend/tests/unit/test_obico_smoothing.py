"""Unit tests for Obico detection smoothing math.

The reference is obico-server ``backend/lib/prediction.py`` (``update_prediction_with_detections``,
``is_failing``) with ``FD_1ST_GEN_PARAMS``. The reference functions are restated here in the plainest
form, and the port is compared with them frame by frame.
"""

import random

import pytest

from backend.app.services.obico_smoothing import (
    BASE_HIGH,
    BASE_LOW,
    ESCALATING_FACTOR,
    SENSITIVITY_MULT,
    WARMUP_FRAMES,
    Baseline,
    PrintState,
    Reading,
    classify,
    score_from_detections,
    thresholds,
)


class TestThresholds:
    def test_medium_matches_base(self):
        low, high = thresholds("medium")
        assert low == pytest.approx(BASE_LOW)
        assert high == pytest.approx(BASE_HIGH)

    def test_low_sensitivity_is_stricter(self):
        low, high = thresholds("low")
        assert low > BASE_LOW
        assert high > BASE_HIGH

    def test_high_sensitivity_is_looser(self):
        low, high = thresholds("high")
        assert low < BASE_LOW
        assert high < BASE_HIGH

    def test_unknown_falls_back_to_medium(self):
        assert thresholds("bogus") == thresholds("medium")


class TestScoreFromDetections:
    def test_empty(self):
        assert score_from_detections([]) == 0.0
        assert score_from_detections(None) == 0.0

    def test_sums_confidences(self):
        dets = [["failure", 0.3, [0, 0, 10, 10]], ["failure", 0.5, [0, 0, 10, 10]]]
        assert score_from_detections(dets) == pytest.approx(0.8)

    def test_ignores_malformed(self):
        dets = [["failure", 0.4, []], ["bad"], ["failure", "nan", []]]
        assert score_from_detections(dets) == pytest.approx(0.4)


# ---- Obico's own arithmetic, restated ----


class _ObicoPrediction:
    def __init__(self):
        self.current_frame_num = 0
        self.lifetime_frame_num = 0
        self.ewm_mean = 0.0
        self.rolling_mean_short = 0.0
        self.rolling_mean_long = 0.0

    def new_print(self):
        self.current_frame_num = 0
        self.ewm_mean = 0.0
        self.rolling_mean_short = 0.0

    def update(self, p):
        def rolling(mean, count, win):
            return mean + (p - mean) / float(win if win <= count else count + 1)

        alpha = 2 / (12 + 1)
        self.current_frame_num += 1
        self.lifetime_frame_num += 1
        self.ewm_mean = p * alpha + self.ewm_mean * (1 - alpha)
        self.rolling_mean_short = rolling(self.rolling_mean_short, self.current_frame_num, 310)
        self.rolling_mean_long = rolling(self.rolling_mean_long, self.lifetime_frame_num, 7200)

    def is_failing(self, sensitivity, escalating_factor=1):
        if self.current_frame_num < 30:
            return False
        adjusted = (self.ewm_mean - self.rolling_mean_long) * sensitivity / escalating_factor
        if adjusted < 0.38:
            return False
        if adjusted > 0.78:
            return True
        return adjusted > (self.rolling_mean_short - self.rolling_mean_long) * 3.8

    def verdict(self, sensitivity):
        if self.is_failing(sensitivity, 1.75):
            return "failure"
        return "warning" if self.is_failing(sensitivity) else "safe"


def _streams():
    rng = random.Random(3459)
    quiet = [0.0] * 400
    step = [0.0] * 200 + [1.2] * 200
    ramp = [i / 300 for i in range(600)]
    background = [0.9] * 500
    noisy = [max(0.0, rng.gauss(0.3, 0.3)) for _ in range(900)]
    bursts = [1.5 if (i // 40) % 3 == 0 else 0.05 for i in range(900)]
    return {"quiet": quiet, "step": step, "ramp": ramp, "background": background, "noisy": noisy, "bursts": bursts}


@pytest.mark.parametrize("name", sorted(_streams()))
@pytest.mark.parametrize("sensitivity", sorted(SENSITIVITY_MULT))
def test_the_port_gives_obicos_verdict_on_every_frame(name, sensitivity):
    """Two prints in a row on one printer, so the carried baseline is compared as well."""
    reference = _ObicoPrediction()
    baseline = Baseline()
    obico_sensitivity = 1 / SENSITIVITY_MULT[sensitivity]
    seen = set()
    for _print in range(2):
        reference.new_print()
        state = PrintState(baseline=baseline)
        for frame, p in enumerate(_streams()[name]):
            reference.update(p)
            reading = state.update(p)
            assert reading.score == pytest.approx(reference.ewm_mean - reference.rolling_mean_long), frame
            verdict = classify(reading, sensitivity)
            assert verdict == reference.verdict(obico_sensitivity), (name, frame)
            seen.add(verdict)
    if name in ("step", "ramp", "bursts"):
        assert seen == {"safe", "warning", "failure"}


class TestWhatTheScoreMeans:
    def test_what_the_camera_always_sees_is_not_a_failure(self):
        """Upstream #3459: a plate texture the model scores at 1.0 on every frame."""
        state = PrintState()
        verdicts = {classify(state.update(1.0), "medium") for _ in range(WARMUP_FRAMES + 600)}
        assert verdicts == {"safe"}

    def test_a_detection_that_appears_and_stays_is_a_failure(self):
        state = PrintState()
        for _ in range(300):
            state.update(0.0)
        verdicts = [classify(state.update(1.2), "medium") for _ in range(40)]
        assert verdicts[0] == "safe"
        assert verdicts[-1] == "failure"

    def test_pausing_takes_a_stronger_signal_than_alerting(self):
        """Between the alert and the pause level, with a short window that has caught up."""
        low, high = thresholds("medium")
        between = Reading(score=(high + high * ESCALATING_FACTOR) / 2, short_rise=10.0, warmup=False)
        assert classify(between, "medium") == "warning"
        above = Reading(score=high * ESCALATING_FACTOR + 0.01, short_rise=10.0, warmup=False)
        assert classify(above, "medium") == "failure"
        assert classify(Reading(score=low - 0.01, short_rise=0.0, warmup=False), "medium") == "safe"

    def test_a_slow_build_up_between_the_thresholds_is_not_failing(self):
        low, high = thresholds("medium")
        middle = (low + high) / 2
        assert classify(Reading(score=middle, short_rise=middle, warmup=False), "medium") == "safe"
        assert classify(Reading(score=middle, short_rise=middle / 10, warmup=False), "medium") == "warning"

    def test_the_first_frames_of_a_print_are_safe_whatever_they_show(self):
        baseline = Baseline()
        for _ in range(500):
            baseline.add(0.0)
        state = PrintState(baseline=baseline)
        verdicts = [classify(state.update(3.0), "medium") for _ in range(WARMUP_FRAMES + 5)]
        assert set(verdicts[: WARMUP_FRAMES - 1]) == {"safe"}
        assert verdicts[-1] == "failure"

    def test_a_new_print_keeps_the_printers_baseline(self):
        baseline = Baseline()
        first = PrintState(baseline=baseline)
        for _ in range(400):
            first.update(0.8)
        second = PrintState(baseline=baseline)
        readings = [second.update(0.8) for _ in range(WARMUP_FRAMES + 20)]
        assert second.frame_count == WARMUP_FRAMES + 20
        assert baseline.frames == 400 + WARMUP_FRAMES + 20
        assert abs(readings[-1].score) < 0.05
