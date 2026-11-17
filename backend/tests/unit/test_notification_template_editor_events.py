"""The template editor knows every editable event: its name, variables and sample.

Seventeen editable templates had no variable list (no chips, an empty preview)
and nine events were shown under their raw key (upstream dad7fea37, 7f64e2ba8).
"""

import re

import pytest

from backend.app.api.routes.notification_templates import event_display_name
from backend.app.models.notification_template import DEFAULT_TEMPLATES
from backend.app.schemas.notification_template import EVENT_VARIABLES, SAMPLE_DATA

EDITABLE = [t["event_type"] for t in DEFAULT_TEMPLATES]


@pytest.mark.parametrize("event", EDITABLE)
def test_every_editable_event_lists_its_variables(event):
    assert EVENT_VARIABLES.get(event), event


@pytest.mark.parametrize("template", DEFAULT_TEMPLATES, ids=EDITABLE)
def test_the_default_template_uses_only_listed_variables(template):
    used = set(re.findall(r"\{(\w+)\}", template["title_template"] + " " + template["body_template"]))
    assert used <= set(EVENT_VARIABLES[template["event_type"]])


@pytest.mark.parametrize("event", EDITABLE)
def test_the_preview_has_a_sample_for_every_variable(event):
    assert set(EVENT_VARIABLES[event]) <= set(SAMPLE_DATA.get(event, {}))


@pytest.mark.parametrize("event", sorted(EVENT_VARIABLES))
def test_no_event_is_shown_under_its_raw_key(event):
    assert event_display_name(event) != event


def test_an_unknown_event_falls_back_to_its_key():
    assert event_display_name("no_such_event") == "no_such_event"
