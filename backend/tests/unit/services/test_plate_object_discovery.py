"""Evidence-ranked discovery of the objects on a printed plate.

slice_info.config is wrong in BOTH directions — measured across 1385 archived
3MFs: OrcaSlicer 2.4+ lists only the source object when a plate holds N copies
(control file: 1 listed, 5 printed), and one file listed two ids that exist in
no other source. So the parse order is gcode header -> pick PNG -> slice_info,
first non-empty wins, never a union.
"""

import io
import struct
import zipfile
import zlib

from backend.app.services.archive import discover_plate_objects


def _png(rows: list[list[tuple[int, int, int]]]) -> bytes:
    """Minimal RGB PNG encoder — avoids shipping binary fixtures."""
    h, w = len(rows), len(rows[0])
    raw = b"".join(b"\x00" + b"".join(bytes(p) for p in row) for row in rows)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def make_3mf(*, slice_ids, gcode_ids=None, pick_ids=None, grey=False, plate_index=1) -> bytes:
    """Build a tiny sliced-3MF shaped file. ~700 bytes; no external fixtures.

    slice_ids: {id: name} written into slice_info.config
    gcode_ids: list written as "; model label id: a,b,c" (None = no gcode entry)
    pick_ids:  list painted as id-coloured bands in pick_1.png (None = no PNG)
    grey:      paint plain greys instead of id colours — reproduces the 5 archived
               files whose pick PNG is an ordinary render, not an id mask.
    """
    objects = "".join(f'<object identify_id="{i}" name="{n}" skipped="false"/>' for i, n in slice_ids.items())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "Metadata/slice_info.config",
            '<?xml version="1.0"?><config><plate>'
            f'<metadata key="index" value="{plate_index}"/>{objects}</plate></config>',
        )
        if gcode_ids is not None:
            ids = ",".join(str(i) for i in gcode_ids)
            z.writestr(f"Metadata/plate_{plate_index}.gcode", f"; HEADER\n; model label id: {ids}\nG28\n")
        if pick_ids is not None:
            size = 40
            rows = [[(0, 0, 0)] * size for _ in range(size)]
            for n, oid in enumerate(pick_ids):
                colour = (100 + n * 30,) * 3 if grey else (oid & 0xFF, (oid >> 8) & 0xFF, (oid >> 16) & 0xFF)
                for y in range(2 + n * 9, 10 + n * 9):
                    for x in range(4, 36):
                        rows[y][x] = colour
            z.writestr(f"Metadata/pick_{plate_index}.png", _png(rows))
    return buf.getvalue()


def _open(data: bytes) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(data))


class TestSliceInfoTier:
    def test_returns_slice_info_objects_when_nothing_else_exists(self):
        data = make_3mf(slice_ids={941: "part.stl", 942: "other.stl"})
        with _open(data) as zf:
            assert discover_plate_objects(zf, 1) == {941: "part.stl", 942: "other.stl"}

    def test_pre_skipped_objects_are_excluded(self):
        """slice_info marks objects the operator already skipped in the slicer."""
        data = make_3mf(slice_ids={941: "part.stl"}).replace(
            b'identify_id="941" name="part.stl" skipped="false"',
            b'identify_id="941" name="part.stl" skipped="true"',
        )
        with _open(data) as zf:
            assert discover_plate_objects(zf, 1) == {}

    def test_empty_when_no_slice_info(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("nothing.txt", "x")
        with _open(buf.getvalue()) as zf:
            assert discover_plate_objects(zf, 1) == {}

    def test_non_numeric_identify_id_is_skipped(self):
        data = make_3mf(slice_ids={941: "ok.stl"}).replace(b'identify_id="941"', b'identify_id="abc"')
        with _open(data) as zf:
            assert discover_plate_objects(zf, 1) == {}


class TestPickPngTier:
    def test_pick_png_beats_slice_info_when_it_has_more(self):
        """The Orca 2.4+ case: one entry listed, several instances printed."""
        data = make_3mf(slice_ids={941: "part.stl"}, pick_ids=[941, 942, 943])
        with _open(data) as zf:
            assert discover_plate_objects(zf, 1) == {
                941: "part.stl",
                942: "part.stl",
                943: "part.stl",
            }

    def test_greyscale_png_is_rejected_entirely(self):
        """Five archived files have an ordinary grey render named pick_1.png.

        Their 'ids' decode to RGB(59,59,59), (117,117,117) and friends — perfect
        greys, far above any pixel-count noise floor. Trusting them invents four
        objects that do not exist, so r == g == b is rejected outright.
        """
        data = make_3mf(slice_ids={941: "part.stl"}, pick_ids=[941, 942, 943], grey=True)
        with _open(data) as zf:
            assert discover_plate_objects(zf, 1) == {941: "part.stl"}

    def test_unknown_id_inherits_the_nearest_smaller_name(self):
        data = make_3mf(slice_ids={100: "small.stl", 200: "big.stl"}, pick_ids=[100, 150, 200, 250])
        with _open(data) as zf:
            found = discover_plate_objects(zf, 1)
        assert found[150] == "small.stl"
        assert found[250] == "big.stl"

    def test_id_below_every_known_one_takes_the_first_name(self):
        data = make_3mf(slice_ids={200: "big.stl"}, pick_ids=[100, 200])
        with _open(data) as zf:
            assert discover_plate_objects(zf, 1)[100] == "big.stl"

    def test_no_slice_info_names_at_all(self):
        buf = io.BytesIO()
        rows = [[(0, 0, 0)] * 40 for _ in range(40)]
        for y in range(4, 20):
            for x in range(4, 36):
                rows[y][x] = (77, 0, 0)
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("Metadata/pick_1.png", _png(rows))
        with _open(buf.getvalue()) as zf:
            assert discover_plate_objects(zf, 1) == {77: "Object_77"}

    def test_tiny_specks_are_ignored(self):
        """Anti-aliasing fringe must not become an object."""
        buf = io.BytesIO()
        rows = [[(0, 0, 0)] * 40 for _ in range(40)]
        for y in range(4, 20):
            for x in range(4, 36):
                rows[y][x] = (77, 0, 0)
        rows[30][30] = (200, 0, 0)  # single stray pixel
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("Metadata/pick_1.png", _png(rows))
        with _open(buf.getvalue()) as zf:
            assert set(discover_plate_objects(zf, 1)) == {77}


class TestGcodeHeaderTier:
    def test_gcode_header_wins_over_both_other_sources(self):
        """Measured: the header was right in all three disputed archives."""
        data = make_3mf(slice_ids={941: "part.stl"}, gcode_ids=[941, 942], pick_ids=[941, 942, 943])
        with _open(data) as zf:
            assert set(discover_plate_objects(zf, 1)) == {941, 942}

    def test_header_can_list_fewer_than_slice_info(self):
        """swapmod_FUCS: slice_info claimed 12, header and pick agreed on 10."""
        data = make_3mf(slice_ids={1: "a", 2: "b", 3: "c"}, gcode_ids=[2, 3])
        with _open(data) as zf:
            assert set(discover_plate_objects(zf, 1)) == {2, 3}

    def test_names_come_from_slice_info_where_available(self):
        data = make_3mf(slice_ids={941: "part.stl"}, gcode_ids=[941, 942, 943])
        with _open(data) as zf:
            found = discover_plate_objects(zf, 1)
        assert found == {941: "part.stl", 942: "part.stl", 943: "part.stl"}

    def test_malformed_header_falls_through(self):
        data = make_3mf(slice_ids={941: "part.stl"}, pick_ids=[941, 942])
        with zipfile.ZipFile(io.BytesIO(data)) as src:
            payload = {n: src.read(n) for n in src.namelist()}
        payload["Metadata/plate_1.gcode"] = b"; HEADER\n; model label id: \nG28\n"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for n, b in payload.items():
                z.writestr(n, b)
        with _open(buf.getvalue()) as zf:
            assert set(discover_plate_objects(zf, 1)) == {941, 942}

    def test_reads_only_the_head_of_a_large_gcode(self):
        """The marker sits at byte ~234; never decompress a 34 MB entry for it."""
        big = b"; HEADER\n; model label id: 941,942\n" + b"G1 X1 Y1\n" * 400_000
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(
                "Metadata/slice_info.config",
                '<?xml version="1.0"?><config><plate><metadata key="index" value="1"/>'
                '<object identify_id="941" name="part.stl" skipped="false"/></plate></config>',
            )
            z.writestr("Metadata/plate_1.gcode", big)
        with _open(buf.getvalue()) as zf:
            assert set(discover_plate_objects(zf, 1)) == {941, 942}

    def test_falls_back_to_the_only_gcode_entry(self):
        """A sliced 3MF has exactly one gcode, whatever plate index it declares."""
        data = make_3mf(slice_ids={5: "x"}, gcode_ids=[5, 6], plate_index=1)
        with _open(data) as zf:
            assert set(discover_plate_objects(zf, 3)) == {5, 6}

    def test_single_object_bambu_studio_file_has_no_header_and_needs_none(self):
        """BS refuses to write the header for one object — GCode.cpp:2344 requires
        num_object_instances() > 1. All 45 header-less files in the corpus are this
        shape, and a one-object plate cannot be undercounted, so tier 3 answering
        is correct rather than a degradation.
        """
        data = make_3mf(slice_ids={941: "part.stl"})
        with _open(data) as zf:
            assert discover_plate_objects(zf, 1) == {941: "part.stl"}
