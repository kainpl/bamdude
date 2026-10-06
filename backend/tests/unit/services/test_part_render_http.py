import json
import urllib.error
import urllib.request
from io import BytesIO
from pathlib import Path

import pytest

from backend.app.services.part_render_http import JobLimits, JobServer, bundle_allowlist


@pytest.fixture
def bundle(tmp_path: Path) -> Path:
    root = tmp_path / "bundle"
    (root / ".vite").mkdir(parents=True)
    (root / "assets").mkdir()
    (root / "index.html").write_text("<!doctype html><script type=module src=./assets/entry.js></script>")
    (root / "assets" / "entry.js").write_text("console.log('page')")
    (root / "assets" / "unlisted.js").write_text("console.log('not in the manifest')")
    (root / ".vite" / "manifest.json").write_text(
        json.dumps({"index.html": {"file": "assets/entry.js", "isEntry": True, "src": "index.html"}})
    )
    return root


@pytest.fixture
def server(bundle: Path, tmp_path: Path):
    out = tmp_path / "out"
    out.mkdir()
    gcode = b"; filament_colour = #00AE42\nG1 X1 E1\n"
    srv = JobServer(
        bundle_dir=bundle,
        job={"size": 512, "objects": [{"id": 101, "mode": "toolpath"}]},
        open_gcode=lambda: BytesIO(gcode),
        gcode_bytes=len(gcode),
        out_dir=out,
        limits=JobLimits(png_bytes=64, total_bytes=100),
    )
    base = srv.start()
    yield srv, base
    srv.close()


def _get(url: str) -> tuple[int, bytes]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""


def _post(url: str, body: bytes, content_type: str = "application/octet-stream") -> int:
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": content_type})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def test_allowlist_is_the_entry_and_manifest_files(bundle: Path):
    assert bundle_allowlist(bundle) == frozenset({"index.html", "assets/entry.js"})


def test_binds_loopback_only(server):
    srv, base = server
    assert base.startswith("http://127.0.0.1:")
    assert srv.origin == base.removeprefix("http://").split("/", 1)[0]


def test_serves_entry_and_listed_assets(server):
    _, base = server
    assert _get(base)[0] == 200
    assert _get(base + "index.html")[0] == 200
    assert _get(base + "assets/entry.js")[1] == b"console.log('page')"


def test_refuses_unlisted_files_traversal_and_foreign_tokens(server):
    _, base = server
    assert _get(base + "assets/unlisted.js")[0] == 404
    assert _get(base + ".vite/manifest.json")[0] == 404
    assert _get(base + "../index.html")[0] == 404
    origin = base.rsplit("/", 2)[0]
    assert _get(origin + "/not-the-token/index.html")[0] == 404


def test_serves_job_and_gcode(server):
    _, base = server
    assert json.loads(_get(base + "job.json")[1]) == {"size": 512, "objects": [{"id": 101, "mode": "toolpath"}]}
    assert _get(base + "plate.gcode")[1].startswith(b"; filament_colour")


def test_png_for_an_unknown_id_is_refused(server):
    srv, base = server
    assert _post(base + "png/999", b"\x89PNG") == 404
    assert srv.outcome.pngs == {}


def test_png_over_the_per_file_cap_is_refused_and_not_written(server):
    srv, base = server
    assert _post(base + "png/101", b"x" * 65) == 413
    assert srv.outcome.pngs == {}


def test_total_cap_counts_every_upload(server):
    srv, base = server
    assert _post(base + "png/101", b"x" * 60) == 204
    assert _post(base + "png/101", b"x" * 60) == 413


def test_manifest_completes_the_job(server):
    srv, base = server
    assert _post(base + "png/101", b"\x89PNGdata") == 204
    assert _post(base + "manifest", json.dumps({"renderer": 1, "objects": []}).encode(), "application/json") == 204
    assert srv.wait(1.0)
    assert srv.outcome.manifest == {"renderer": 1, "objects": []}
    assert srv.outcome.pngs[101].read_bytes() == b"\x89PNGdata"


def test_error_post_completes_the_job(server):
    srv, base = server
    assert (
        _post(base + "error", json.dumps({"reason": "parse_failed", "message": "x"}).encode(), "application/json")
        == 204
    )
    assert srv.wait(1.0)
    assert srv.outcome.error == {"reason": "parse_failed", "message": "x"}


def test_wait_times_out_without_an_answer(server):
    srv, _ = server
    assert srv.wait(0.05) is False
