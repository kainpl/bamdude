"""Painted multi-colour models keep their colours (upstream a3542b8fe, issue #3355).

Before a slice, the filament profiles of slots the plate does not use are
replaced with one it does use, so the slicer's material and printer checks see
a homogeneous set. Which slots a plate uses includes the filaments PAINTED onto
its triangles, and ``paint_color`` was read one hex digit at a time as "digit =
filament number". It is BambuStudio's TriangleSelector tree: four bits a node,
written last node first, and filament 1 is the string ``4``, filament 2 ``8``,
filament 3 ``0C``. Painted filaments therefore counted as unused and took
another slot's profile AND colour — a black/white/green model sliced all green.

The 3MFs here are built by hand from the attribute strings; no slicer runs.
``_encode`` is an independent transcription of ``TriangleSelector::serialize``
+ ``FacetsAnnotation::get_triangle_as_string`` (BambuStudio v02.09.01.70,
``src/libslic3r/TriangleSelector.cpp`` / ``Model.cpp``), so the decoder is
checked against the writer's rules rather than against itself.
"""

from __future__ import annotations

import json
import zipfile
from io import BytesIO

import pytest

from backend.app.services.slicer_3mf_convert import substitute_unused_plate_filaments
from backend.app.utils.threemf_tools import _painted_states, extract_plate_extruder_set_from_3mf

# --------------------------------------------------------------------- writer


def _encode(node) -> str:
    """``paint_color`` of one triangle. A node is a leaf state (0 = unpainted,
    N = filament N) or ``(special_side, [children])`` with 2-4 children."""
    nibbles: list[int] = []

    def emit(n) -> None:
        if isinstance(n, int):
            if n < 3:
                nibbles.append(n << 2)  # split_sides = 0 in the low bits
                return
            nibbles.append(0b1100)
            rest = n - 3
            while rest >= 15:
                nibbles.append(0b1111)
                rest -= 15
            nibbles.append(rest)
            return
        special_side, children = n
        split_sides = len(children) - 1
        assert 1 <= split_sides <= 3
        nibbles.append(split_sides | (special_side << 2))
        for child in reversed(children):  # "serialized in reverse order"
            emit(child)

    emit(node)
    return "".join("0123456789ABCDEF"[n] for n in reversed(nibbles))  # digits are prepended


class TestTheWriterTranscription:
    """The encoder above against strings the slicer is known to write."""

    @pytest.mark.parametrize(
        ("state", "text"),
        [(1, "4"), (2, "8"), (3, "0C"), (4, "1C"), (17, "EC"), (18, "0FC"), (32, "EFC"), (33, "0FFC")],
    )
    def test_whole_triangle_paints(self, state, text):
        assert _encode(state) == text


# -------------------------------------------------------------------- decoder


class TestPaintedStates:
    @pytest.mark.parametrize(
        ("text", "states"),
        [
            (b"4", {1}),
            (b"8", {2}),
            (b"0C", {3}),
            (b"1C", {4}),
            (b"EC", {17}),
            (b"0FC", {18}),
            (b"0FFC", {33}),
            (b"0", set()),
        ],
    )
    def test_a_whole_triangle(self, text, states):
        assert _painted_states(text) == states

    @pytest.mark.parametrize("state", range(1, 65))
    def test_every_filament_number_reads_back(self, state):
        assert _painted_states(_encode(state).encode()) == {state}

    def test_a_split_triangle_yields_every_painted_leaf_and_no_split_code(self):
        """A three-way split whose first child is split again: the node codes
        (``3`` and ``1`` here) are structure, never a filament."""
        text = _encode((0, [(0, [4, 3]), 5, 3, 0]))

        assert _painted_states(text.encode()) == {3, 4, 5}

    def test_the_string_from_bambustudios_own_source(self):
        """``Model.cpp`` builds ``"1C0C2C0C1C13"`` when it converts a legacy
        paint: a 3-split whose first written child is a 1-split of filaments 4
        and 3, then filaments 5, 3 and 4. The encoder above writes the same."""
        assert _encode((0, [4, 3, 5, (0, [3, 4])])) == "1C0C2C0C1C13"
        assert _painted_states(b"1C0C2C0C1C13") == {3, 4, 5}

    def test_a_deep_tree_of_two_filaments(self):
        tree = (2, [1, (1, [0, 2, 1]), (0, [(3, [2, 2, 0, 1]), 0]), 2])
        assert _painted_states(_encode(tree).encode()) == {1, 2}

    def test_an_unpainted_split_paints_nothing(self):
        assert _painted_states(_encode((0, [0, 0, 0, 0])).encode()) == set()

    def test_the_old_reading_is_gone(self):
        """One digit is not one filament: ``4`` is filament 1, never slot 4."""
        assert 4 not in _painted_states(b"4")
        assert 8 not in _painted_states(b"8")
        assert 12 not in _painted_states(b"0C")

    def test_a_string_that_ends_early_keeps_what_was_read(self):
        whole = _encode((0, [1, 2, 0, 0]))  # digits are read from the END of the string
        assert whole == "48003"

        assert _painted_states(b"8003") == {2}  # the last child never arrives
        assert _painted_states(b"C") == set()  # the prefix of a long state, and nothing after it
        assert _painted_states(b"FC") == set()

    def test_digits_after_the_tree_is_complete_are_ignored(self):
        assert _painted_states(b"84") == {1}  # "4" is the whole tree; "8" is never reached

    def test_lower_case_is_not_a_paint_string(self):
        """BambuStudio drops the whole triangle on anything but 0-9A-F."""
        assert _painted_states(b"0c") == set()


# ------------------------------------------------------------- the 3MF reader


def _model_settings(objects: dict[int, int], plates: dict[int, list[int]], parts: dict[int, list[int]] | None = None):
    """``objects``: object id → its extruder; ``plates``: plater id → object ids."""
    xml = ["<config>"]
    for oid, extruder in objects.items():
        xml.append(f'<object id="{oid}"><metadata key="extruder" value="{extruder}"/>')
        for index, part_extruder in enumerate((parts or {}).get(oid, []), start=1):
            xml.append(f'<part id="{index}"><metadata key="extruder" value="{part_extruder}"/></part>')
        xml.append("</object>")
    for plater_id, object_ids in plates.items():
        xml.append(f'<plate><metadata key="plater_id" value="{plater_id}"/>')
        for oid in object_ids:
            xml.append(f'<model_instance><metadata key="object_id" value="{oid}"/></model_instance>')
        xml.append("</plate>")
    xml.append("</config>")
    return "".join(xml)


_NS = (
    'xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
    'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06"'
)


def _root_model(components: dict[int, str]) -> str:
    """``3D/3dmodel.model`` as BambuStudio writes it: each object is a
    component pointing at its own ``3D/Objects/object_N.model``."""
    objects = "".join(
        f'<object id="{oid}" type="model"><components><component p:path="/{path}" objectid="1"/></components></object>'
        for oid, path in components.items()
    )
    return f'<?xml version="1.0"?><model unit="millimeter" {_NS}><resources>{objects}</resources></model>'


def _object_model(paints: list[str | None]) -> str:
    triangles = "".join(
        f'<triangle v1="0" v2="1" v3="2"{"" if paint is None else f" paint_color={chr(34)}{paint}{chr(34)}"}/>'
        for paint in paints
    )
    return (
        f'<?xml version="1.0"?><model unit="millimeter" {_NS}><resources><object id="1" type="model"><mesh>'
        f'<vertices><vertex x="0" y="0" z="0"/><vertex x="1" y="0" z="0"/><vertex x="0" y="1" z="0"/></vertices>'
        f"<triangles>{triangles}</triangles></mesh></object></resources></model>"
    )


def _three_mf(entries: dict[str, str]) -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, text in entries.items():
            zf.writestr(name, text)
    return buf.getvalue()


def _used(source: bytes, plate: int) -> set[int]:
    with zipfile.ZipFile(BytesIO(source)) as zf:
        return extract_plate_extruder_set_from_3mf(zf, plate)


def _painted_cube(paints: list[str | None], *, extruder: int = 3) -> bytes:
    """One object on plate 1, printed with ``extruder`` and painted as given."""
    return _three_mf(
        {
            "Metadata/model_settings.config": _model_settings({1: extruder}, {1: [1]}),
            "3D/3dmodel.model": _root_model({1: "3D/Objects/object_1.model"}),
            "3D/Objects/object_1.model": _object_model(paints),
        }
    )


class TestThePlatesUsedSlots:
    def test_the_reported_model(self):
        """Issue #3355: the object prints with filament 3, filaments 1 and 2
        are painted on. The old reading answered {3, 4, 8}."""
        source = _painted_cube([None, None, "4", "4", "8", "8", None])

        assert _used(source, 1) == {1, 2, 3}

    def test_a_split_triangle_counts_its_leaves(self):
        source = _painted_cube([_encode((0, [1, 0, 4, 0]))], extruder=2)

        assert _used(source, 1) == {1, 2, 4}

    def test_one_painted_triangle_among_hundreds_still_makes_its_filament_used(self):
        """No noise threshold: the slicer prints that facet with that filament."""
        source = _painted_cube(["4"] * 400 + ["8"], extruder=1)

        assert _used(source, 1) == {1, 2}

    def test_paint_on_another_plates_object_is_not_this_plates(self):
        source = _three_mf(
            {
                "Metadata/model_settings.config": _model_settings({1: 1, 2: 1}, {1: [1], 2: [2]}),
                "3D/3dmodel.model": _root_model({1: "3D/Objects/object_1.model", 2: "3D/Objects/object_2.model"}),
                "3D/Objects/object_1.model": _object_model([None]),
                "3D/Objects/object_2.model": _object_model(["8", "0C"]),
            }
        )

        assert _used(source, 1) == {1}
        assert _used(source, 2) == {1, 2, 3}

    def test_a_mesh_kept_inside_the_root_model_is_read_too(self):
        """Not every writer splits objects into ``3D/Objects``."""
        root = (
            f'<?xml version="1.0"?><model unit="millimeter" {_NS}><resources><object id="1" type="model"><mesh>'
            '<triangles><triangle v1="0" v2="1" v3="2" paint_color="8"/></triangles></mesh></object>'
            '<object id="2" type="model"><mesh><triangles><triangle v1="0" v2="1" v3="2" paint_color="0C"/>'
            "</triangles></mesh></object></resources></model>"
        )
        source = _three_mf(
            {
                "Metadata/model_settings.config": _model_settings({1: 1, 2: 1}, {1: [1], 2: [2]}),
                "3D/3dmodel.model": root,
            }
        )

        assert _used(source, 1) == {1, 2}
        assert _used(source, 2) == {1, 3}


class TestAnUnpaintedFileReadsAsBefore:
    """Per-object and per-part filaments — nothing here depends on paint."""

    def test_object_and_part_extruders(self):
        source = _three_mf(
            {
                "Metadata/model_settings.config": _model_settings(
                    {1: 1, 2: 4}, {1: [1], 2: [2]}, parts={1: [1, 3], 2: [4]}
                ),
                "3D/3dmodel.model": _root_model({1: "3D/Objects/object_1.model", 2: "3D/Objects/object_2.model"}),
                "3D/Objects/object_1.model": _object_model([None, None]),
                "3D/Objects/object_2.model": _object_model([None]),
            }
        )

        assert _used(source, 1) == {1, 3}
        assert _used(source, 2) == {4}

    def test_no_model_settings_is_no_information(self):
        assert _used(_three_mf({"3D/3dmodel.model": "<model/>"}), 1) == set()


# ----------------------------------------------------------- the substitution


def _profile(name: str, kind: str, colour: str) -> str:
    return json.dumps(
        {
            "name": name,
            "filament_type": [kind],
            "filament_colour": [colour],
            "nozzle_temperature": ["220"],
            "compatible_printers": ["Bambu Lab A1 0.4 nozzle"],
        }
    )


BLACK = _profile("Bambu PLA Basic @BBL A1", "PLA", "#000000")
WHITE = _profile("Bambu PLA Matte @BBL A1", "PLA", "#FFFFFF")
GREEN = _profile("Generic PETG @BBL A1", "PETG", "#00AE42")
ABS = _profile("Bambu ABS @BBL H2D", "ABS", "#FF6A13")


def _without_colour(profile: str) -> dict:
    data = json.loads(profile)
    data.pop("filament_colour")
    return data


class TestThePaintedModelKeepsItsColours:
    def test_the_reported_slice(self):
        """Black / white / green / orange; the object is green and painted
        black and white. Slots 1-3 are used and stay exactly as picked; the
        unused fourth takes the anchor's profile and keeps its own colour.
        Before: slots 1 and 2 took slot 3's profile and its green."""
        source = _painted_cube(["4", "8", None])
        picks = [BLACK, WHITE, GREEN, ABS]

        out = substitute_unused_plate_filaments(source, 1, picks)

        assert out[:3] == [BLACK, WHITE, GREEN]
        assert [json.loads(p)["filament_colour"] for p in out] == [["#000000"], ["#FFFFFF"], ["#00AE42"], ["#FF6A13"]]
        assert _without_colour(out[3]) == _without_colour(BLACK)

    def test_a_painted_slot_keeps_its_colour_even_if_it_were_taken_for_unused(self):
        """Whatever the reader makes of a file, the swap is for the slicer's
        material and printer checks, which never read the colour."""
        source = _three_mf({"Metadata/model_settings.config": _model_settings({1: 3}, {1: [1]})})

        out = substitute_unused_plate_filaments(source, 1, [BLACK, WHITE, GREEN])

        assert [json.loads(p)["filament_colour"] for p in out] == [["#000000"], ["#FFFFFF"], ["#00AE42"]]
        assert _without_colour(out[0]) == _without_colour(GREEN)


class TestAnUnpaintedMultiFilamentFileSlicesAsBefore:
    """Per-part filaments, no paint: which slots are used, which slot anchors,
    and what profile an unused slot is given are what they were."""

    SOURCE = _three_mf(
        {
            "Metadata/model_settings.config": _model_settings({1: 2}, {1: [1]}, parts={1: [2, 3]}),
            "3D/3dmodel.model": _root_model({1: "3D/Objects/object_1.model"}),
            "3D/Objects/object_1.model": _object_model([None, None, None]),
        }
    )

    def test_the_used_slots_are_the_declared_ones(self):
        assert _used(self.SOURCE, 1) == {2, 3}

    def test_used_slots_are_passed_through_untouched(self):
        picks = [ABS, WHITE, GREEN, BLACK]

        out = substitute_unused_plate_filaments(self.SOURCE, 1, picks)

        assert out[1] is picks[1]
        assert out[2] is picks[2]

    def test_unused_slots_take_the_lowest_used_slots_profile(self):
        """The anchor is slot 2 (the lowest used), not slot 1."""
        out = substitute_unused_plate_filaments(self.SOURCE, 1, [ABS, WHITE, GREEN, BLACK])

        assert _without_colour(out[0]) == _without_colour(WHITE)
        assert _without_colour(out[3]) == _without_colour(WHITE)

    def test_an_unused_slot_of_the_anchors_colour_is_the_anchors_profile_verbatim(self):
        same_colour = _profile("Bambu ABS @BBL H2D", "ABS", "#FFFFFF")
        picks = [same_colour, WHITE, GREEN]

        out = substitute_unused_plate_filaments(self.SOURCE, 1, picks)

        assert out[0] is picks[1]

    def test_profiles_that_are_not_json_are_copied_as_they_always_were(self):
        out = substitute_unused_plate_filaments(self.SOURCE, 1, ["abs", "pla", "petg", "pva"])

        assert out == ["pla", "pla", "petg", "pla"]

    def test_another_plate_or_no_plate_changes_nothing(self):
        picks = [ABS, WHITE, GREEN]

        assert substitute_unused_plate_filaments(self.SOURCE, None, picks) is picks
        assert substitute_unused_plate_filaments(self.SOURCE, 7, picks) == picks
