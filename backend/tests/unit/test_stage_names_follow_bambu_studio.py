"""The print-stage names mirror BambuStudio's ``get_stage_string`` (upstream 73e0787b).

Upstream papered over an unnamed stage 74 with "Preparing". BamDude mirrors the
table instead (BambuStudio is the protocol authority for gcode stage strings):
the firmware names 67-76 and BambuStudio has the words for them. Our own 74 was
labelled "Preparing" from a guess on an H2D, while it is the heatbed foreign-
object check; and 50 said "Cooling heatbed" where BambuStudio now says the bed is
being adjusted either way. Source: BambuStudio v02.08.02.61,
src/slic3r/GUI/DeviceManager.cpp ``get_stage_string``.
"""

import pytest

from backend.app.services.bambu_mqtt import STAGE_NAMES

BAMBU_STUDIO = {
    50: "Adjusting heatbed temperature",
    67: "Measuring Rotary Attachment",
    68: "The toolhead moves above the purge chute",
    69: "Cooling down the nozzle",
    70: "The toolhead moves to the center of the heatbed",
    71: "Active Arc Fitting",
    72: "Hotend Type Detection",
    73: "Build plate alignment detection",
    74: "Heatbed surface foreign object detection",
    75: "Heatbed underside foreign object detection",
    76: "Pre-extrusion before printing",
    77: "Preparing AMS",
}


@pytest.mark.parametrize(("stage", "name"), sorted(BAMBU_STUDIO.items()))
def test_the_stage_is_named_as_bambu_studio_names_it(stage, name):
    assert STAGE_NAMES.get(stage) == name
