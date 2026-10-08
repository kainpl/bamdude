"""part_render_protocol: one home for the part-render constants and the packed result (plan E3, task 13)."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]


def test_the_renderer_version_is_one_number_in_three_places():
    from backend.app.services import part_render_node, part_render_protocol as prp

    ts = (ROOT / "frontend" / "src" / "part-render" / "protocol.ts").read_text(encoding="utf-8")
    assert int(re.search(r"export const RENDERER_VERSION = (\d+)", ts).group(1)) == prp.RENDERER_VERSION
    assert part_render_node.RENDERER_VERSION == prp.RENDERER_VERSION
    assert part_render_node.HEAP_MB == prp.NODE_HEAP_MB


def test_a_crashed_attempt_and_the_next_one_fit_the_bucket():
    from backend.app.services import part_render_node, part_render_protocol as prp

    assert 2 * prp.ATTEMPT_BYTES <= prp.BUCKET_BYTES
    # Node's stdout, a small PNG per instance and the manifest leave room for the top_mask masters, which
    # are native-size crops of one top_N.png (task 15)
    assert (
        prp.NODE_OUTPUT_BYTES + prp.INSTANCE_CAP * prp.SMALL_PNG_BYTES + part_render_node.MANIFEST_MAX
        <= prp.ATTEMPT_BYTES - 2 * 1024**2
    )


def test_the_reason_sets_are_disjoint():
    from backend.app.services import part_render_protocol as prp

    flat = [
        r
        for group in (prp.DEGRADED, prp.TRANSIENT, prp.TERMINAL_FAILED, prp.UNAVAILABLE, prp.RUNTIME_UNAVAILABLE)
        for r in group
    ]
    assert len(flat) == len(set(flat))
    assert set(prp.RENDERED) < set(prp.METHODS)


FILES = {"manifest.json": b'{"objects":[]}', "101.lg.png": b"\x89PNG-lg", "101.sm.png": b"\x89PNG-sm"}


def _packed(tmp_path: Path) -> Path:
    from backend.app.services.part_render_protocol import pack

    pack(FILES, tmp_path / "result.bin")
    return tmp_path / "result.bin"


def _out(tmp_path: Path) -> Path:
    out = tmp_path / "out"
    out.mkdir()
    return out


def test_a_packed_result_round_trips(tmp_path):
    from backend.app.services.part_render_protocol import unpack

    got = unpack(_packed(tmp_path), _out(tmp_path))
    assert set(got) == set(FILES)
    assert all((tmp_path / "out" / name).read_bytes() == body for name, body in FILES.items())


@pytest.mark.parametrize("name", ["../x.png", "a.png", "101.lg.png/../x", "1.big.png", "manifest.json\x00"])
def test_pack_refuses_a_name_outside_the_pattern(tmp_path, name):
    from backend.app.services.part_render_protocol import PackError, pack

    with pytest.raises(PackError):
        pack({**FILES, name: b"x"}, tmp_path / "result.bin")


def test_pack_refuses_more_than_the_limit(tmp_path):
    from backend.app.services.part_render_protocol import PackError, pack

    with pytest.raises(PackError):
        pack(FILES, tmp_path / "result.bin", limit=16)


@pytest.mark.parametrize(
    ("old", "new"),
    [(b"\x89PNG-lg", b"\x89PNG-LG"), (b'"bytes":7', b'"bytes":6')],
    ids=["changed body", "shorter declared size"],
)
def test_unpack_refuses_bytes_that_do_not_match_the_header(tmp_path, old, new):
    from backend.app.services.part_render_protocol import PackError, unpack

    packed = _packed(tmp_path)
    data = packed.read_bytes()
    assert old in data
    packed.write_bytes(data.replace(old, new, 1))
    with pytest.raises(PackError):
        unpack(packed, _out(tmp_path))


def test_unpack_refuses_trailing_bytes(tmp_path):
    from backend.app.services.part_render_protocol import PackError, unpack

    packed = _packed(tmp_path)
    packed.write_bytes(packed.read_bytes() + b"x")
    with pytest.raises(PackError):
        unpack(packed, _out(tmp_path))


def test_unpack_requires_a_manifest(tmp_path):
    from backend.app.services.part_render_protocol import PackError, pack, unpack

    pack({"101.lg.png": b"\x89PNG"}, tmp_path / "result.bin")
    with pytest.raises(PackError):
        unpack(tmp_path / "result.bin", _out(tmp_path))


def test_pack_refuses_a_png_over_its_ceiling(tmp_path):
    from backend.app.services.part_render_protocol import PNG_BYTES, PackError, pack

    with pytest.raises(PackError, match="exceeds its ceiling"):
        pack({"manifest.json": b"{}", "1.lg.png": b"\x89PNG" + b"0" * PNG_BYTES}, tmp_path / "result.bin")


def test_unpack_refuses_a_png_over_its_ceiling_in_a_hand_made_pack(tmp_path):
    """The input of unpack is untrusted: a pack that pack() would never write, refused by unpack's own ceiling
    (consilium E3.2-R5) -- the total stays inside ATTEMPT_BYTES, only the one PNG is too big."""
    import hashlib
    import json
    import struct

    from backend.app.services import part_render_protocol as prp

    body, manifest = b"\x89PNG" + b"0" * prp.PNG_BYTES, b"{}"
    header = json.dumps(
        {
            "files": [
                {"name": "1.lg.png", "bytes": len(body), "sha256": hashlib.sha256(body).hexdigest()},
                {"name": "manifest.json", "bytes": len(manifest), "sha256": hashlib.sha256(manifest).hexdigest()},
            ]
        }
    ).encode()
    (tmp_path / "result.bin").write_bytes(prp._MAGIC + struct.pack(">I", len(header)) + header + body + manifest)
    assert (tmp_path / "result.bin").stat().st_size < prp.ATTEMPT_BYTES
    with pytest.raises(prp.PackError, match="1.lg.png: size outside its ceiling"):
        prp.unpack(tmp_path / "result.bin", _out(tmp_path))
