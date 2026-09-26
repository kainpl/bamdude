"""The printer's echo of a project_file command is a first-class sighting of it.

⚠️ Measured on hardware 2026-08-21, from an operator's own MQTT recording.

An X1 **disconnects** when BamDude subscribes to its request topic, so BamDude
disables that subscription for it — permanently, via a class-level cache. The
slicer's dispatch is therefore never seen there, and a print started from
BambuStudio was picked up with no AMS mapping and no plate at all.

But the printer echoes the whole command back on the *report* topic, which we
already read: every field, plus ``result``/``reason``. It arrived at 15:32:53
for a print that started at 15:33:03 — ten seconds of margin.

The payload below is that recording, trimmed of nothing that matters.
"""

import pytest

from backend.app.services.bambu_mqtt import BambuMQTTClient, is_successful_project_file

#: Verbatim from ``logs/mqtt/mqtt-20260821-1.log``, direction ``in``, topic
#: ``device/<serial>/report``. ``sequence_id`` 20008 — BamDude pins its own
#: project_file sends to "20000", so this one is Studio's.
ECHOED_COMMAND = {
    "print": {
        "ams_mapping": [1],
        "ams_mapping2": [{"ams_id": 0, "slot_id": 1}],
        "bed_type": "textured_plate",
        "command": "project_file",
        "file": "Cube_slicer.gcode.3mf",
        "param": "Metadata/plate_1.gcode",
        "sequence_id": "20008",
        "subtask_name": "Cube_slicer",
        "url": "ftp://Cube_slicer.gcode.3mf",
        "use_ams": True,
        "reason": "success",
        "result": "success",
    }
}


@pytest.fixture
def client():
    """A client with nothing captured yet, and no broker behind it."""
    return BambuMQTTClient.__new__(BambuMQTTClient)


def _blank(client):
    client.serial_number = "01P09C4B3002022"
    client._captured_ams_mapping = None
    client._captured_print_param = None
    client._own_project_file_keys = {}

    class _State:
        current_project_url = None
        last_project_url = None

    client.state = _State()
    return client


class TestTheEchoIsRead:
    def test_the_mapping_is_captured(self, client):
        _blank(client)
        client._handle_project_file_command(ECHOED_COMMAND)
        assert client._captured_ams_mapping == [1]

    def test_the_plate_is_captured(self, client):
        """``param`` is the plate, and here it is the only sighting of it."""
        _blank(client)
        client._handle_project_file_command(ECHOED_COMMAND)
        assert client._captured_print_param == "Metadata/plate_1.gcode"

    def test_the_captured_param_parses_to_the_plate(self, client):
        """The end of the chain: what main.py does with it."""
        from backend.app.services.printer_manager import parse_plate_id

        _blank(client)
        client._handle_project_file_command(ECHOED_COMMAND)
        assert parse_plate_id(client._captured_print_param) == 1

    def test_a_command_without_a_plate_captures_nothing(self, client):
        _blank(client)
        client._handle_project_file_command({"print": {"command": "project_file", "param": ""}})
        assert client._captured_print_param is None

    def test_another_command_is_ignored(self, client):
        _blank(client)
        client._handle_project_file_command({"print": {"command": "gcode_line", "param": "Metadata/plate_9.gcode"}})
        assert client._captured_print_param is None
        assert client._captured_ams_mapping is None


class TestOnlyASuccessfulDispatchIsBelieved:
    """⚠️ The report topic carries refusals too, in the same shape.

    A refused dispatch starts no print, so a mapping captured from it would sit
    there waiting to be attributed to whatever runs next — a print it has
    nothing to do with.
    """

    def test_a_success_is_taken(self):
        assert is_successful_project_file(ECHOED_COMMAND["print"]) is True

    def test_a_refusal_is_not(self):
        refused = dict(ECHOED_COMMAND["print"], result="fail", reason="device busy")
        assert is_successful_project_file(refused) is False

    def test_the_request_topic_form_has_no_result_and_is_still_taken(self):
        """⚠️ The slicer's own send carries no ``result`` — absence is success."""
        as_sent = {k: v for k, v in ECHOED_COMMAND["print"].items() if k not in ("result", "reason")}
        assert is_successful_project_file(as_sent) is True

    def test_an_upper_case_success_is_taken(self):
        """An H2S echoes ``result: "SUCCESS"`` (upstream 5dd7bd21); BambuStudio
        lower-cases the result before comparing. Compared as-is, every such echo
        read as a refusal and the report topic never yielded the mapping."""
        assert is_successful_project_file(dict(ECHOED_COMMAND["print"], result="SUCCESS")) is True

    def test_a_refusal_in_any_case_is_not(self):
        assert is_successful_project_file(dict(ECHOED_COMMAND["print"], result="FAIL")) is False

    def test_a_status_push_is_not_a_dispatch(self):
        assert is_successful_project_file({"command": "push_status", "result": "success"}) is False

    def test_a_non_dict_envelope_is_refused(self):
        assert is_successful_project_file("project_file") is False


def test_the_key_the_client_sends_is_the_key_main_reads():
    """⚠️ One bare string crossing a module boundary, in three places.

    ``bambu_mqtt`` puts the captured plate into the print-start payload under
    ``plate_param``; ``main.on_print_start`` reads it back out by that name to
    resolve ``live_plate_id``. Nothing type-checks the hop — renaming either
    side leaves a print silently back to no plate at all, which is exactly the
    bug this was written for.
    """
    import inspect

    from backend.app import main
    from backend.app.services import bambu_mqtt

    sent = inspect.getsource(bambu_mqtt).count('"plate_param": self._captured_print_param')
    assert sent == 2, f"expected both print-start callbacks to carry it, found {sent}"
    assert 'parse_plate_id(data.get("plate_param"))' in inspect.getsource(main)


class TestOurOwnDispatchIsRecognised:
    """Our dispatch is told from a slicer's by what it was, not by a magic
    ``sequence_id`` (upstream 89ea3376).

    "20000" is not ours alone: slicers count up from the same base (measured:
    Orca 20000 then 20001, Studio 20009/20010), so whichever slicer dispatch
    landed on it was filed as ours and never logged. The job we actually sent is
    remembered and consumed on its echo — once PER TOPIC, because both topics
    feed the same handler and our dispatch is echoed on each.
    """

    OURS = {
        "sequence_id": "20000",
        "command": "project_file",
        "param": "Metadata/plate_1.gcode",
        "url": "ftp://Lamp.gcode.3mf",
        "file": "Lamp.gcode.3mf",
        "subtask_name": "Lamp",
    }

    @staticmethod
    def _external_logs(caplog) -> int:
        return sum("External project_file payload" in r.getMessage() for r in caplog.records)

    def test_our_echo_on_both_topics_is_not_called_external(self, client, caplog):
        _blank(client)
        client._remember_own_project_file(dict(self.OURS))
        with caplog.at_level("INFO", logger="backend.app.services.bambu_mqtt"):
            client._handle_project_file_command({"print": dict(self.OURS)}, source="request")
            client._handle_project_file_command({"print": dict(self.OURS, result="SUCCESS")}, source="report")
        assert self._external_logs(caplog) == 0

    def test_a_slicer_dispatch_on_the_shared_sequence_id_is_logged(self, client, caplog):
        _blank(client)
        client._remember_own_project_file(dict(self.OURS))
        slicer = dict(self.OURS, file="Cube.gcode.3mf", url="ftp://Cube.gcode.3mf", subtask_name="Cube")
        with caplog.at_level("INFO", logger="backend.app.services.bambu_mqtt"):
            client._handle_project_file_command({"print": slicer}, source="request")
        assert self._external_logs(caplog) == 1

    def test_the_marker_is_one_shot(self, client, caplog):
        _blank(client)
        """A slicer reprint of the same file a moment later is somebody else's."""
        client._remember_own_project_file(dict(self.OURS))
        with caplog.at_level("INFO", logger="backend.app.services.bambu_mqtt"):
            client._handle_project_file_command({"print": dict(self.OURS)}, source="request")
            client._handle_project_file_command({"print": dict(self.OURS)}, source="request")
        assert self._external_logs(caplog) == 1

    def test_the_dispatch_remembers_what_it_publishes(self):
        import inspect
        import re

        source = re.sub(r"\s+", " ", inspect.getsource(BambuMQTTClient.start_print))
        remember = source.index('self._remember_own_project_file(command["print"])')
        publish = source.index("self._client.publish(self.topic_publish, json.dumps(command), qos=1)")
        assert remember < publish
