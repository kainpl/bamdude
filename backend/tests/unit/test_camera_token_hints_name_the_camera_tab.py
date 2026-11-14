"""Camera, Cam Wall, monitor and overlay tokens are created under Settings → Camera (review M4).

The token panel and the overlay builder moved there from Settings → API Keys; a refusal or a notice that
still sends people to API Keys for such a token sends them to a tab that no longer has it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[2] / "app"


def test_the_token_refusals_point_at_the_camera_tab():
    source = (APP / "core" / "auth.py").read_text(encoding="utf-8")

    assert "Settings > API Keys" not in source
    assert "Create one under Settings > Camera with the 'Cam Wall' scope." in source
    assert '"Valid overlay token required. Create one under Settings > Camera "' in source


def test_their_ukrainian_names_the_tab_as_the_app_does():
    catalog = json.loads((APP / "data" / "api_errors_uk.json").read_text(encoding="utf-8"))

    hints = {key: value for key, value in catalog.items() if "Create one under Settings >" in key}
    assert len(hints) == 2
    for key, value in hints.items():
        assert "Settings > Camera" in key, key
        assert "Камери" in value and "API" not in value, value


@pytest.mark.parametrize(("language", "tab"), [("en", "Settings → Camera"), ("uk", "Налаштуваннях → Камери")])
def test_the_retirement_notice_sends_tokens_to_the_camera_tab(language, tab):
    text = (APP / "data" / f"notification_templates_{language}.json").read_text(encoding="utf-8")
    bodies = [line for line in text.splitlines() if "long_lived_token_count" in line and "body_template" in line]

    assert bodies, "the credential retirement notice is gone — repoint this test"
    for body in bodies:
        assert tab in body
