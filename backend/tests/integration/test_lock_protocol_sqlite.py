"""Scenario f of the lock protocol on a file-backed SQLite (WS-13 E1, spec T1 5).

SQLite's gate is its single write lock, taken before the first read. A waiting
write must recheck its proposal after a group is added. After fresh confirmation,
the write retains that group's choice. A group added after a write lands on the
updated line. The recipe is "A to its end, uncommitted; B waits; A commits".
"""

import pytest

from backend.tests.integration.test_postgres_scenarios import _run

pytestmark = pytest.mark.integration


def test_configuration_writers_stay_fresh_behind_the_sqlite_write_lock(tmp_path_factory):
    r = _run("protocol:f", tmp_path_factory.mktemp("sqlite_protocol_f"), None)
    for pair in ("group_then_configuration", "configuration_then_group", "group_then_new_line"):
        assert r[pair]["b_waited"], (pair, r[pair])
    for pair in ("configuration_then_group", "group_then_new_line"):
        assert (r[pair]["a"], r[pair]["b"]) == ("ok", "ok"), (pair, r[pair])
    g = r["group_then_configuration"]
    assert g["a"] == "ok" and g["b"].startswith("http:409:") and "line_configuration_stale" in g["b"], g
    assert g["retry"] == "ok" and g["choice_unchanged_after_conflict"], g
    assert g["has_size"] and g["kept_blue"] and g["key_ok"] and g["kit_unchanged_by_a_new_standard"], g
    c = r["configuration_then_group"]
    assert c["has_size"] and c["kept_blue"] and c["key_ok"], c
    assert r["group_then_new_line"]["new_line_has_size"], r["group_then_new_line"]
    won = r["choice_then_delete"]
    assert won["a"] == "ok" and won["b"].startswith("http:409:") and "chosen in" in won["b"], won
    lost = r["delete_then_choice"]
    assert lost["a"] == "ok" and lost["b"].startswith("http:422:"), lost
    assert lost["dangling"] == 0, lost
