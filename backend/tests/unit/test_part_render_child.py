"""part_render: the disposable child and the only reader of the source (spec §5.6; plan E3, task 16)."""

import hashlib
import io
import json
import os
import time
from pathlib import Path

import pytest
from PIL import Image

from backend.app import part_render
from backend.app.services.part_render_protocol import MODEL_PROBE_ID, unpack
from backend.tests.fixtures.part_render_3mf import (
    gcode,
    pick_png,
    single_object_3mf,
    top_png,
    two_objects_3mf,
    write_3mf,
)
from backend.tests.unit.services.test_part_render_node import NODE, needs_node


def boot_for(
    tmp_path: Path,
    source: Path,
    *,
    plate: int = 1,
    node: str | None = None,
    kind: str = "3mf",
    sha256: str | None = None,
    size: int | None = None,
    root: Path | None = None,
    deadline_s: float = 120,
) -> dict:
    attempt = tmp_path / "attempt"
    attempt.mkdir(exist_ok=True)
    data = source.read_bytes()
    return {
        "root": str(attempt),
        "task": {
            "path": str(source),
            "root": str(root or source.parent),
            "kind": kind,
            "sha256": sha256 or hashlib.sha256(data).hexdigest(),
            "size": len(data) if size is None else size,
            "plate_index": plate,
        },
        "deadline_ns": time.monotonic_ns() + int(deadline_s * 1e9),
        "node": node,
    }


def manifest_of(tmp_path: Path) -> tuple[dict, Path]:
    out = tmp_path / "out"
    out.mkdir()
    unpack(tmp_path / "attempt" / "result.bin", out)
    return json.loads((out / "manifest.json").read_text(encoding="utf-8")), out


def methods(manifest: dict) -> dict[int, tuple[str, str | None]]:
    return {o["identify_id"]: (o["method"], o["reason"]) for o in manifest["objects"]}


@needs_node
def test_two_marked_objects_render_by_their_markers(tmp_path):
    source = two_objects_3mf(tmp_path / "f.3mf")
    result, reader_alive = part_render.render_attempt(boot_for(tmp_path, source, node=NODE))
    assert (result["outcome"], result["reason"], reader_alive) == ("ok", None, False)
    manifest, out = manifest_of(tmp_path)
    assert methods(manifest) == {101: ("toolpath", None), 202: ("toolpath", None)}
    assert Image.open(out / "101.lg.png").size == (512, 512)
    assert Image.open(out / "101.sm.png").size == (128, 128)
    assert json.loads((tmp_path / "attempt" / "node.pid").read_text(encoding="ascii"))["pid"] > 0


@needs_node
def test_a_single_unmarked_object_renders_as_model_in_one_pass(tmp_path, monkeypatch):
    passes = []
    real = part_render.run_node

    def counted(*args, **kwargs):
        passes.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(part_render, "run_node", counted)
    result, _ = part_render.render_attempt(boot_for(tmp_path, single_object_3mf(tmp_path / "f.3mf"), node=NODE))
    manifest, _ = manifest_of(tmp_path)
    assert result["outcome"] == "ok"
    assert methods(manifest) == {1: ("model", None)}
    assert passes == [1]  # one read of the G-code, not a second one for the model (plan E3, R3)


def test_without_node_every_instance_falls_back_to_the_pair(tmp_path):
    result, _ = part_render.render_attempt(boot_for(tmp_path, two_objects_3mf(tmp_path / "f.3mf")))
    manifest, out = manifest_of(tmp_path)
    assert result == {"outcome": "ok", "reason": None, "methods": result["methods"]}
    assert methods(manifest) == {101: ("top_mask", None), 202: ("top_mask", None)}
    assert Image.open(out / "101.sm.png").size == (128, 128)


def test_a_refused_pair_leaves_missing_no_valid_pair(tmp_path):
    same = top_png()
    source = write_3mf(
        tmp_path / "f.3mf", {1: gcode("two-objects")}, objects={1: {101: "A", 202: "B"}}, top={1: same}, pick={1: same}
    )
    part_render.render_attempt(boot_for(tmp_path, source))
    manifest, _ = manifest_of(tmp_path)
    assert methods(manifest) == {101: ("missing", "no_valid_pair"), 202: ("missing", "no_valid_pair")}


@pytest.mark.parametrize("field", ["sha256", "size"])
def test_content_that_does_not_match_the_key_is_source_changed(tmp_path, field):
    source = two_objects_3mf(tmp_path / "f.3mf")
    wrong = {"sha256": "0" * 64} if field == "sha256" else {"size": 1}
    result, _ = part_render.render_attempt(boot_for(tmp_path, source, **wrong))
    assert result == {"outcome": "failed", "reason": "source_changed"}
    assert not (tmp_path / "attempt" / "result.bin").exists()


def test_a_file_outside_its_root_is_source_changed(tmp_path):
    other = tmp_path / "elsewhere"
    other.mkdir()
    result, _ = part_render.render_attempt(boot_for(tmp_path, two_objects_3mf(tmp_path / "f.3mf"), root=other))
    assert result == {"outcome": "failed", "reason": "source_changed"}


def test_a_file_that_vanished_is_source_read_failed(tmp_path):
    source = two_objects_3mf(tmp_path / "f.3mf")
    boot = boot_for(tmp_path, source)
    source.unlink()
    result, _ = part_render.render_attempt(boot)
    assert result == {"outcome": "failed", "reason": "source_read_failed"}


def test_a_plate_that_is_not_in_the_file_is_no_gcode(tmp_path):
    result, _ = part_render.render_attempt(boot_for(tmp_path, two_objects_3mf(tmp_path / "f.3mf"), plate=2))
    assert result == {"outcome": "unavailable", "reason": "no_gcode"}


def test_plate_zero_of_a_multi_plate_file_is_no_gcode(tmp_path):
    source = write_3mf(tmp_path / "f.3mf", {1: gcode("two-objects"), 2: gcode("two-objects")})
    result, _ = part_render.render_attempt(boot_for(tmp_path, source, plate=0))
    assert result == {"outcome": "unavailable", "reason": "no_gcode"}


def test_plate_zero_of_a_single_plate_file_is_its_only_plate(tmp_path):
    result, _ = part_render.render_attempt(boot_for(tmp_path, two_objects_3mf(tmp_path / "f.3mf"), plate=0))
    manifest, _ = manifest_of(tmp_path)
    assert result["outcome"] == "ok"
    assert set(methods(manifest)) == {101, 202}


def test_a_plate_without_objects_is_no_objects(tmp_path):
    source = write_3mf(tmp_path / "f.3mf", {1: gcode("single-no-markers")})  # no header, no slice_info, no pick
    result, _ = part_render.render_attempt(boot_for(tmp_path, source))
    assert result == {"outcome": "unavailable", "reason": "no_objects"}


def test_gcode_over_the_ceiling_never_starts_node(tmp_path):
    # a node that does not exist would fail the attempt; ok proves it was never started
    boot = boot_for(tmp_path, two_objects_3mf(tmp_path / "f.3mf"), node=str(tmp_path / "no-node"))
    result, _ = part_render.render_attempt(boot, limits=part_render.Limits(gcode_bytes=10))
    manifest, _ = manifest_of(tmp_path)
    assert (result["outcome"], result["reason"]) == ("ok", "too_large")
    assert methods(manifest) == {101: ("top_mask", None), 202: ("top_mask", None)}
    assert not (tmp_path / "attempt" / "node.pid").exists()


def test_the_cap_takes_one_of_each_name_first():
    names = [f"Part {chr(97 + i // 26)}{chr(97 + i % 26)}" for i in range(300)]
    objects = dict.fromkeys(range(1, 301), "Bolt") | {1000 + i: name for i, name in enumerate(names)}
    chosen, skipped = part_render.select_instances(objects, 256)
    assert len(chosen) == 256 and len(skipped) == 344
    assert 1 in chosen  # the smallest Bolt: "bolt" sorts before every "part .."
    assert set(range(2, 301)).isdisjoint(chosen)  # one of each name comes before a second copy
    assert part_render.select_instances(objects, 256) == (chosen, skipped)


def test_the_cap_fills_the_rest_by_id_once_every_name_has_one():
    assert part_render.select_instances({1: "A", 2: "A", 3: "A", 10: "B", 11: "B"}, 4) == ([1, 2, 3, 10], [11])


def test_skipped_instances_are_over_cap(tmp_path):
    part_render.render_attempt(
        boot_for(tmp_path, two_objects_3mf(tmp_path / "f.3mf")), limits=part_render.Limits(instance_cap=1)
    )
    manifest, _ = manifest_of(tmp_path)
    assert methods(manifest)[202] == ("skipped", "over_cap")


def test_the_model_probe_rides_with_a_single_object():
    plan = part_render.PlatePlan({7: "X"}, [7], [], 10, model_bbox=[0.0, 0.0, 1.0, 1.0])
    job = part_render.job_for(plan, part_render.Limits())
    assert [(o["id"], o["mode"]) for o in job["objects"]] == [(7, "toolpath"), (MODEL_PROBE_ID, "model")]


def test_the_model_probe_is_skipped_when_a_real_id_collides():
    plan = part_render.PlatePlan({MODEL_PROBE_ID: "X"}, [MODEL_PROBE_ID], [], 10, model_bbox=[0.0, 0.0, 1.0, 1.0])
    job = part_render.job_for(plan, part_render.Limits())
    assert [o["mode"] for o in job["objects"]] == ["toolpath"]


def test_marker_scan_sees_a_marker_split_across_chunks():
    scan = part_render.MarkerScan({202})
    line = b"; start printing object, unique label id: 202\n"
    scan.feed(b"G1 X1\n" + line[:20])
    scan.feed(line[20:] + b"G1 X2\n")
    assert scan.marked == {202}


@needs_node
def test_a_stream_error_mid_plate_is_source_read_failed(tmp_path, monkeypatch):
    real = part_render._zip_chunks

    def broken(zf, entry, cap, scan):
        chunks = real(zf, entry, cap, scan)
        yield next(chunks)
        raise OSError("the share went away")

    monkeypatch.setattr(part_render, "_zip_chunks", broken)
    result, _ = part_render.render_attempt(boot_for(tmp_path, two_objects_3mf(tmp_path / "f.3mf"), node=NODE))
    assert result == {"outcome": "failed", "reason": "source_read_failed"}
    assert not (tmp_path / "attempt" / "result.bin").exists()


@needs_node
def test_an_unmarked_instance_beside_marked_ones_falls_back_to_the_pair(tmp_path):
    """Spec §5.3 row 2: marked ids render by their markers, the unmarked one takes top_mask."""
    body = gcode("two-objects").replace(b"; model label id: 101,202", b"; model label id: 101,202,303")
    source = write_3mf(
        tmp_path / "f.3mf",
        {1: body},
        objects={1: {101: "Bracket", 202: "Clip", 303: "Pin"}},
        top={1: top_png()},
        pick={1: pick_png({101: (4, 4, 20, 20), 202: (30, 30, 50, 50), 303: (52, 4, 60, 12)})},
    )
    part_render.render_attempt(boot_for(tmp_path, source, node=NODE))
    manifest, _ = manifest_of(tmp_path)
    assert methods(manifest) == {101: ("toolpath", None), 202: ("toolpath", None), 303: ("top_mask", None)}


@needs_node
def test_a_palette_without_a_used_colour_is_parse_failed_with_the_fallback_in_the_same_attempt(tmp_path):
    """Spec §5.3: deterministic, so no retry -- ready with reason parse_failed, the instances by the pair."""
    body = gcode("two-objects").replace(b"#C0C0C0;#161616;#00AE42", b"#C0C0C0")  # T2 is used and has no colour
    source = write_3mf(
        tmp_path / "f.3mf",
        {1: body},
        objects={1: {101: "Bracket", 202: "Clip"}},
        top={1: top_png()},
        pick={1: pick_png({101: (4, 4, 20, 20), 202: (30, 30, 50, 50)})},
    )
    result, _ = part_render.render_attempt(boot_for(tmp_path, source, node=NODE))
    manifest, _ = manifest_of(tmp_path)
    assert (result["outcome"], result["reason"]) == ("ok", "parse_failed")
    assert methods(manifest) == {101: ("top_mask", None), 202: ("top_mask", None)}


@needs_node
def test_a_heap_oom_is_memory_limit_with_the_fallback_in_the_same_attempt(tmp_path, monkeypatch):
    real = part_render.node_command
    monkeypatch.setattr(part_render, "node_command", lambda node: real(node, heap_mb=16))
    lines = b"".join(b"G1 X%d Y%d E0.1\n" % (i % 200, (i // 200) % 200) for i in range(1_500_000))
    source = write_3mf(
        tmp_path / "f.3mf",
        {1: gcode("two-objects") + b"; start printing object, unique label id: 101\n" + lines},
        objects={1: {101: "Bracket", 202: "Clip"}},
        top={1: top_png()},
        pick={1: pick_png({101: (4, 4, 20, 20), 202: (30, 30, 50, 50)})},
    )
    result, _ = part_render.render_attempt(boot_for(tmp_path, source, node=NODE))
    assert (result["outcome"], result["reason"]) == ("ok", "memory_limit")


@needs_node
def test_a_raw_gcode_with_a_label_header_renders_its_objects(tmp_path):
    source = tmp_path / "f.gcode"
    source.write_bytes(gcode("two-objects"))
    result, _ = part_render.render_attempt(boot_for(tmp_path, source, plate=0, kind="gcode", node=NODE))
    manifest, _ = manifest_of(tmp_path)
    assert result["outcome"] == "ok"
    assert methods(manifest) == {101: ("toolpath", None), 202: ("toolpath", None)}


def test_a_raw_gcode_on_a_positive_plate_is_no_gcode(tmp_path):
    source = tmp_path / "f.gcode"
    source.write_bytes(gcode("two-objects"))
    result, _ = part_render.render_attempt(boot_for(tmp_path, source, plate=1, kind="gcode"))
    assert result == {"outcome": "unavailable", "reason": "no_gcode"}


def test_a_stuck_reader_makes_the_child_exit_hard_after_its_result(tmp_path, monkeypatch):
    attempt = tmp_path / "attempt"
    attempt.mkdir()
    boot = {"root": str(attempt), "task": {}, "deadline_ns": 0, "node": None}

    class Stdin:
        buffer = io.BytesIO(json.dumps(boot).encode() + b"\n")

    exits: list[int] = []
    monkeypatch.setattr(part_render.sys, "stdin", Stdin())
    monkeypatch.setattr(part_render, "render_attempt", lambda boot: ({"outcome": "failed", "reason": "timeout"}, True))
    monkeypatch.setattr(part_render.os, "_exit", exits.append)
    part_render.main()
    assert exits == [0]
    assert json.loads((attempt / "result.json").read_text(encoding="utf-8")) == {
        "outcome": "failed",
        "reason": "timeout",
    }
    assert not (attempt / "child.pid").exists()  # its guardian records it, before its bootstrap (task 17)


DISCOVERY_ENTRIES = ["Metadata/plate_1.gcode", "Metadata/slice_info.config", "Metadata/pick_1.png"]


@pytest.mark.parametrize("failing", DISCOVERY_ENTRIES)
def test_a_read_error_in_each_discovery_source_is_source_read_failed(tmp_path, monkeypatch, failing):
    """Consilium E3-R5: the share failing during discovery is a retry, never a final no_objects."""
    real = part_render._read_entry

    def flaky(zf, name, limit=None):
        if name == failing:
            raise OSError("the share went away")
        return real(zf, name, limit)

    monkeypatch.setattr(part_render, "_read_entry", flaky)
    result, _ = part_render.render_attempt(boot_for(tmp_path, two_objects_3mf(tmp_path / "f.3mf")))
    assert result == {"outcome": "failed", "reason": "source_read_failed"}


def _reads(monkeypatch) -> list[str]:
    seen: list[str] = []
    real = part_render._read_entry

    def probe(zf, name, limit=None):
        seen.append(name)
        return real(zf, name, limit)

    monkeypatch.setattr(part_render, "_read_entry", probe)
    return seen


def test_an_archive_with_duplicate_names_is_refused_before_any_entry_is_read(tmp_path, monkeypatch):
    import warnings
    import zipfile

    source = two_objects_3mf(tmp_path / "f.3mf")
    with warnings.catch_warnings(), zipfile.ZipFile(source, "a") as zf:
        warnings.simplefilter("ignore")  # zipfile warns about the duplicate; that is the point
        zf.writestr("Metadata/slice_info.config", "<config/>")
    seen = _reads(monkeypatch)
    result, _ = part_render.render_attempt(boot_for(tmp_path, source))
    assert result == {"outcome": "unavailable", "reason": "invalid_archive"}
    assert seen == []


@pytest.mark.parametrize("limits", [{"zip_entries": 2}, {"zip_bytes": 100}], ids=["entries", "bytes"])
def test_an_oversized_archive_is_refused_before_any_entry_is_read(tmp_path, monkeypatch, limits):
    seen = _reads(monkeypatch)
    result, _ = part_render.render_attempt(
        boot_for(tmp_path, two_objects_3mf(tmp_path / "f.3mf")), limits=part_render.Limits(**limits)
    )
    assert result == {"outcome": "unavailable", "reason": "invalid_archive"}
    assert seen == []


def test_a_compression_bomb_beside_the_plate_is_never_decompressed(tmp_path, monkeypatch):
    bomb = b"\0" * (4 * 1024 * 1024)  # deflates about a thousandfold
    source = write_3mf(
        tmp_path / "f.3mf",
        {1: gcode("two-objects")},
        objects={1: {101: "Bracket", 202: "Clip"}},
        top={1: top_png()},
        pick={1: bomb},
    )
    seen = _reads(monkeypatch)
    result, _ = part_render.render_attempt(boot_for(tmp_path, source), limits=part_render.Limits(zip_ratio=100))
    manifest, _ = manifest_of(tmp_path)
    assert "Metadata/pick_1.png" not in seen  # judged on its directory entry, then left alone
    assert result["outcome"] == "ok"  # the objects come from the G-code header and slice_info
    assert methods(manifest) == {101: ("missing", "no_valid_pair"), 202: ("missing", "no_valid_pair")}


def test_model_is_not_taken_for_an_id_whose_marker_passed():
    """Consilium E3-R7 / spec §5.3: a marked id whose selection is empty is top_mask or empty_selection."""
    plan = part_render.PlatePlan({7: "X"}, [7], [], 10, model_bbox=[0.0, 0.0, 1.0, 1.0])
    manifest = {
        "objects": [
            {"id": 7, "method": "missing", "reason": "empty_selection"},
            {"id": MODEL_PROBE_ID, "method": "model", "tools": [0]},
        ]
    }
    (instance,) = part_render.assemble(plan, manifest, {MODEL_PROBE_ID: b"\x89PNG"}, marked={7})
    assert (instance.method, instance.reason) == ("missing", "empty_selection")


def test_model_is_taken_for_a_single_object_without_markers():
    plan = part_render.PlatePlan({7: "X"}, [7], [], 10, model_bbox=[0.0, 0.0, 1.0, 1.0])
    manifest = {
        "objects": [
            {"id": 7, "method": "missing", "reason": "empty_selection"},
            {"id": MODEL_PROBE_ID, "method": "model", "tools": [0]},
        ]
    }
    (instance,) = part_render.assemble(plan, manifest, {MODEL_PROBE_ID: b"\x89PNG"}, marked=set())
    assert instance.method == "model"


def test_a_malformed_bootstrap_is_refused(tmp_path, monkeypatch):
    class Stdin:
        buffer = io.BytesIO(b'{"root": "x"}\n')

    monkeypatch.setattr(part_render.sys, "stdin", Stdin())
    assert part_render.main() == 2


def test_the_marker_scan_keeps_only_the_ids_it_was_asked_about():
    """Security review: a 256 MiB plate may name millions of ids; the scan holds the selected ones only."""
    scan = part_render.MarkerScan({101})
    scan.feed(b"".join(b"; start printing object, unique label id: %d\n" % oid for oid in [101, *range(1000, 20000)]))
    assert scan.marked == {101} and scan.any_marker


def test_a_marker_with_an_absurd_id_is_neither_a_crash_nor_a_match():
    scan = part_render.MarkerScan({1234567890})
    scan.feed(b"; start printing object, unique label id: 12345678901234\n")
    scan.feed(b"; start printing object, unique label id: " + b"9" * 5000 + b"\n")
    assert scan.marked == set()


def test_a_foreign_marker_still_rules_out_the_model_probe():
    """Spec §5.3 / E3-R7: model only when NO marker passed -- an id outside the plan's selection counts."""
    plan = part_render.PlatePlan({7: "X"}, [7], [], 10, model_bbox=[0.0, 0.0, 1.0, 1.0])
    manifest = {
        "objects": [
            {"id": 7, "method": "missing", "reason": "empty_selection"},
            {"id": MODEL_PROBE_ID, "method": "model", "tools": [0]},
        ]
    }
    (instance,) = part_render.assemble(plan, manifest, {MODEL_PROBE_ID: b"\x89PNG"}, marked=set(), any_marker=True)
    assert instance.method != "model"


def test_a_pick_image_over_the_pixel_ceiling_is_not_decoded_for_discovery(tmp_path, monkeypatch):
    """Security review: discovery and the pair skip an oversized pick unread; slice_info still names the objects."""
    import zipfile

    from backend.app.services import part_render_fallback

    monkeypatch.setattr(part_render_fallback, "PAIR_PIXELS", 16)
    seen = []
    real = part_render._discover

    def spy(number, head, slice_info, pick):
        seen.append(pick)
        return real(number, head, slice_info, pick)

    monkeypatch.setattr(part_render, "_discover", spy)
    with zipfile.ZipFile(two_objects_3mf(tmp_path / "f.3mf")) as zf:
        plan, _ = part_render.plan_3mf(zf, 1, part_render.Limits())
    assert seen == [None] and plan.pair is None
    assert set(plan.objects) == {101, 202}


def test_a_raw_gcode_header_with_an_absurd_id_keeps_the_sane_ones(tmp_path):
    source = tmp_path / "plate.gcode"
    source.write_bytes(b"; model label id: 7," + b"9" * 5000 + b"\nG1 X1\n")
    with source.open("rb") as handle:
        plan = part_render.plan_gcode(handle, {"plate_index": 0, "size": source.stat().st_size}, part_render.Limits())
    assert set(plan.objects) == {7}


def test_a_marker_split_inside_its_digits_is_not_two_ids():
    """A chunk boundary inside the digits: the partial id is no marker; the whole one is seen next feed."""
    scan = part_render.MarkerScan({12, 1234})
    line = b"; start printing object, unique label id: 1234\n"
    cut = line.index(b"34")
    scan.feed(b"G1 X1\n" + line[:cut])
    scan.feed(line[cut:] + b"G1 X2\n")
    assert scan.marked == {1234}
