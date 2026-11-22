"""The per-user print-option preference read.

"Nothing saved yet" is an ordinary answer, not an error: the Print dialog and
the calibration page ask it on every open, and a 404 there was logged by every
browser as a failed resource for each user who had never saved one.
"""

import pytest

BODY = {
    "print_options": {
        "bed_levelling": "on",
        "flow_cali": "off",
        "layer_inspect": False,
        "timelapse": True,
        "mesh_mode_fast_check": True,
        "gcode_injection": False,
    },
    "swap_macros": {"execute": False},
}


@pytest.mark.asyncio
async def test_nothing_saved_is_an_empty_answer_not_a_404(async_client):
    response = await async_client.get("/api/v1/print-option-preferences/A1")
    assert response.status_code == 200
    assert response.json() is None


@pytest.mark.asyncio
async def test_a_saved_preference_is_read_back_for_its_model_only(async_client):
    saved = await async_client.put("/api/v1/print-option-preferences/A1", json=BODY)
    assert saved.status_code == 200

    response = await async_client.get("/api/v1/print-option-preferences/A1")
    assert response.status_code == 200
    body = response.json()
    assert body["printer_model"] == "A1"
    assert body["options"]["print_options"]["timelapse"] is True

    other = await async_client.get("/api/v1/print-option-preferences/P1S")
    assert other.status_code == 200
    assert other.json() is None
