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
