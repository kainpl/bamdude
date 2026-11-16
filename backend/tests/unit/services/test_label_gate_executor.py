"""Label drawing has workers of its own.

A renderer handed the worst design the bounds allow works for many seconds, and
closing the connection does not stop a thread. On the loop's default executor a
handful of such requests held every thread the rest of the server works through
— a print gate opening its 3MF, an upload hashing, the preview runtime's disk
calls. So label drawing runs on a small executor of its own, and a request that
cannot be drawn within the ceiling is told the drawing is busy.
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from fastapi import HTTPException

from backend.app.services import label_gate, label_picture
from backend.app.services.label_template import LabelTemplateSpec


@pytest.fixture(autouse=True)
async def _fresh_executor():
    await label_gate.shutdown()
    yield
    await label_gate.shutdown()


async def test_the_shared_thread_pool_stays_free_while_label_draws_are_in_flight():
    release = threading.Event()
    started = threading.Event()

    def slow_render() -> str:
        started.set()
        release.wait(30)
        return "drawn"

    # More draws than the default executor has threads on any host (it caps at 32).
    draws = [asyncio.create_task(label_gate.draw(slow_render)) for _ in range(40)]
    try:
        await asyncio.to_thread(started.wait, 5)
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "free"), 3) == "free"
    finally:
        release.set()
        assert await asyncio.gather(*draws) == ["drawn"] * 40


async def test_no_more_than_the_label_workers_draw_at_once():
    release = threading.Event()
    lock = threading.Lock()
    running = 0
    most = 0

    def slow_render() -> None:
        nonlocal running, most
        with lock:
            running += 1
            most = max(most, running)
        release.wait(30)
        with lock:
            running -= 1

    draws = [asyncio.create_task(label_gate.draw(slow_render)) for _ in range(8)]
    await asyncio.sleep(0.3)
    release.set()
    await asyncio.gather(*draws)
    assert most == label_gate.DRAW_WORKERS == 2


async def test_a_draw_that_cannot_finish_within_the_ceiling_is_told_the_drawing_is_busy(monkeypatch):
    monkeypatch.setattr(label_gate, "DRAW_CEILING_SECONDS", 0.2)
    release = threading.Event()
    ran: list[str] = []

    def hold() -> None:
        release.wait(30)

    def late() -> None:
        ran.append("late")

    holders = [asyncio.create_task(label_gate.draw(hold)) for _ in range(label_gate.DRAW_WORKERS)]
    await asyncio.sleep(0.05)
    with pytest.raises(HTTPException) as refused:
        await label_gate.draw(late)
    assert refused.value.status_code == 503
    assert refused.value.detail == "Label drawing is busy — try again in a moment"

    release.set()
    await asyncio.gather(*holders, return_exceptions=True)
    # The request that gave up left nothing behind in the queue.
    await label_gate.draw(lambda: None)
    assert ran == []


async def test_a_renderer_s_own_error_reaches_the_caller():
    def broken() -> None:
        raise ValueError("no such face")

    with pytest.raises(ValueError, match="no such face"):
        await label_gate.draw(broken)


async def test_the_workers_come_back_after_a_shutdown():
    assert await label_gate.draw(lambda: 1) == 1
    await label_gate.shutdown()
    assert await label_gate.draw(lambda: 2) == 2


def _spec(width_mm: float, height_mm: float) -> LabelTemplateSpec:
    return LabelTemplateSpec(name="T", width_mm=width_mm, height_mm=height_mm, elements=[])


class TestThePixelBudgetOfOneExport:
    def test_the_worst_export_the_count_alone_allowed_is_refused_before_anything_is_drawn(self, monkeypatch):
        def reached(*_args, **_kwargs):
            raise AssertionError("a renderer was reached")

        monkeypatch.setattr(label_picture, "render_pictures", reached)
        with pytest.raises(HTTPException) as refused:
            label_picture.checked(_spec(169.0, 169.0), 600, labels=200)
        assert refused.value.status_code == 422
        assert refused.value.detail == (
            "At 600 dpi these 200 labels come to more than 500 megapixels — select fewer or choose a lower resolution."
        )

    def test_fifty_shipping_labels_at_600_dpi_are_an_export(self):
        label_picture.checked(_spec(100.0, 150.0), 600, labels=50)

    def test_two_hundred_shipping_labels_at_300_dpi_are_an_export(self):
        label_picture.checked(_spec(100.0, 150.0), 300, labels=200)

    def test_one_label_of_the_largest_side_is_still_a_picture(self):
        label_picture.checked(_spec(169.0, 169.0), 600, labels=1)
