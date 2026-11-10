"""A refusal explains the fix without echoing credential-bearing input/context."""

import pytest
from pydantic import ValidationError

from backend.app.schemas.slicer import SliceRequest


@pytest.mark.parametrize(
    ("password", "message"),
    [
        ("lowercase-secret-97831", "Password must contain at least one uppercase letter"),
        ("UPPERCASE-SECRET-97831", "Password must contain at least one lowercase letter"),
        ("NoDigitsSecretCanary", "Password must contain at least one digit"),
    ],
)
async def test_password_refusal_keeps_actionable_message_without_secret_echo(async_client, password, message):
    response = await async_client.post("/api/v1/users/", json={"username": "validation-fixture", "password": password})
    assert response.status_code == 422, response.text
    assert password not in response.text
    details = response.json()["detail"]
    assert len(details) == 1
    assert set(details[0]) == {"type", "loc", "msg"}
    assert details[0] == {"type": "value_error", "loc": ["body", "password"], "msg": f"Value error, {message}"}


async def test_unknown_secret_field_name_and_value_are_not_echoed(async_client):
    secret = "validation-extra-key-canary"
    response = await async_client.patch("/api/v1/settings/", json={secret: secret})
    assert response.status_code == 422
    assert secret not in response.text
    assert response.json()["detail"][0]["loc"] == ["body", "[unknown field]"]
    assert all(set(error) == {"type", "loc", "msg"} for error in response.json()["detail"])


def test_colour_validator_explains_slot_and_format_without_reflecting_user_text():
    secret = "not-a-colour-opaque-canary"
    with pytest.raises(ValidationError) as refusal:
        SliceRequest.model_validate(
            {
                "printer_preset_id": 1,
                "process_preset_id": 2,
                "filament_preset_id": 3,
                "filament_colours": [secret],
            }
        )
    message = refusal.value.errors()[0]["msg"]
    assert "filament_colours[0] must be '#RRGGBB' or '#RRGGBBAA'" in message
    assert secret not in message
