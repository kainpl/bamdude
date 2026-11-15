"""A CAD export is often made of needles: a cylinder's wall as slivers running its whole height, a flat cap as
wedges from the centre to the rim. matplotlib has no depth buffer — it sorts triangles by ONE average depth
and draws back to front — so such meshes used to come out torn: wedges showing through, streaked walls, the
inside of a thin wall painted over its outside. These tests hold the three measures that fix it.
"""

import numpy as np
import pytest

trimesh = pytest.importorskip("trimesh")
pytest.importorskip("matplotlib")
Image = pytest.importorskip("PIL.Image")


def _tube(sections: int = 720):
    """A thin-walled tube whose walls are slivers as tall as the model — closed, wound outward."""
    mesh = trimesh.creation.annulus(r_min=23.8, r_max=25.0, height=100.0, sections=sections)
    assert mesh.is_watertight and mesh.is_winding_consistent and mesh.volume > 0
    return mesh


def _rough_pixels(png_path) -> tuple[int, int]:
    """(flawed, inside): pixels inside the silhouette, in the picture's middle band, that are not a clean wall.

    A lit smooth wall is opaque and changes tone gradually along a row. A seam between two slivers lets the
    background through (the pixel is not fully opaque); a triangle drawn out of order is a jump in tone against
    the pixel beside it. The silhouette's own antialiased edge — two pixels at each end of a row — is left out.
    """
    rgba = np.asarray(Image.open(png_path).convert("RGBA")).astype(int)
    band = rgba[rgba.shape[0] * 2 // 5 : rgba.shape[0] * 3 // 5]
    flawed = inside = 0
    for row in band:
        covered = np.flatnonzero(row[:, 3] > 0)
        if len(covered) < 8:
            continue
        span = row[covered[0] + 2 : covered[-1] - 1]
        tone, alpha = span[:, 1], span[:, 3]
        bad = alpha < 255
        bad[1:] |= np.abs(np.diff(tone)) > 10
        flawed += int(bad.sum())
        inside += len(span)
    return flawed, inside


class TestLongTrianglesAreSplit:
    def test_only_the_long_ones_and_nothing_is_lost(self):
        from backend.app.services.stl_thumbnail import _split_long_triangles

        small = np.array([[[0, 0, 0], [1, 0, 0], [0, 1, 0]]], dtype=float)
        needle = np.array([[[0, 0, 0], [100, 0, 0], [0, 0.5, 0]]], dtype=float)
        out = _split_long_triangles(np.concatenate([small, needle]), max_edge=5.0, max_faces=10_000)

        def area(tri):
            return float(np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1).sum() / 2)

        edges = np.linalg.norm(out - np.roll(out, 1, axis=1), axis=2)
        assert edges.max() <= 5.0
        assert area(out) == pytest.approx(area(small) + area(needle))
        assert (out == small[0]).all(axis=(1, 2)).sum() == 1, "a triangle that is short enough is left as it was"
        # winding survives: every piece of the needle still faces +Z
        assert (np.cross(out[:, 1] - out[:, 0], out[:, 2] - out[:, 0])[:, 2] > 0).all()

    def test_the_face_budget_is_never_exceeded(self):
        from backend.app.services.stl_thumbnail import _split_long_triangles

        needles = np.tile(np.array([[[0, 0, 0], [100, 0, 0], [0, 0.5, 0]]], dtype=float), (50, 1, 1))
        out = _split_long_triangles(needles, max_edge=0.01, max_faces=400)
        assert 50 <= len(out) <= 400

    def test_nothing_to_split_returns_the_same_triangles(self):
        from backend.app.services.stl_thumbnail import _split_long_triangles

        tri = np.array([[[0, 0, 0], [1, 0, 0], [0, 1, 0]]], dtype=float)
        assert _split_long_triangles(tri, max_edge=5.0, max_faces=10) is tri
        assert len(_split_long_triangles(np.empty((0, 3, 3)), max_edge=5.0, max_faces=10)) == 0


class TestFacesTurnedAwayAreNotDrawn:
    def test_a_closed_box_keeps_the_three_sides_the_camera_sees(self):
        from backend.app.services.stl_thumbnail import _facing_camera

        box = trimesh.creation.box(extents=(1, 1, 1))
        kept = _facing_camera(np.asarray(box.triangles))
        assert len(kept) == 6, "top, front and right: two triangles each"
        normals = np.cross(kept[:, 1] - kept[:, 0], kept[:, 2] - kept[:, 0])
        # the camera stands at +x, -y, +z of the model
        assert ((normals[:, 0] > 0) | (normals[:, 1] < 0) | (normals[:, 2] > 0)).all()

    def test_a_face_seen_almost_edge_on_is_kept(self):
        """The view is a perspective: a wall the parallel rule calls edge-on can still show a sliver of itself."""
        from backend.app.services.stl_thumbnail import VIEW_AZIM_DEG, VIEW_ELEV_DEG, _facing_camera

        el, az = np.radians(VIEW_ELEV_DEG), np.radians(VIEW_AZIM_DEG)
        camera = np.array([np.cos(el) * np.cos(az), np.cos(el) * np.sin(az), np.sin(el)])
        side = np.cross(camera, [0.0, 0.0, 1.0])
        side /= np.linalg.norm(side)
        up = np.cross(side, camera)
        # one triangle that plainly faces the camera stays beside the one under test: a mesh of which NOTHING
        # faces the camera is drawn whole rather than as an empty picture
        b = np.cross(camera, up)
        seen = np.array([np.zeros(3), b, np.cross(camera, b)])
        for tilt, expected in ((-0.05, 2), (-0.5, 1)):
            normal = side + tilt * camera  # turned slightly, then clearly, away
            a = np.cross(normal, up)
            turned = np.array([np.zeros(3), a, np.cross(normal, a)])
            assert np.dot(np.cross(turned[1] - turned[0], turned[2] - turned[0]), normal) > 0
            assert len(_facing_camera(np.stack([seen, turned]))) == expected
        assert len(_facing_camera(np.stack([turned]))) == 1, "nothing facing: drawn whole, never an empty picture"

    def test_an_open_or_inside_out_mesh_is_drawn_whole(self):
        """Only a closed mesh wound outward hides its far side; anything else may be showing its inside."""
        from backend.app.services.stl_thumbnail import _hides_its_back

        closed = _tube(sections=24)
        assert _hides_its_back(closed)
        opened = closed.copy()
        opened.update_faces(np.arange(len(opened.faces)) > 3)
        assert not _hides_its_back(opened)
        inverted = closed.copy()
        inverted.invert()
        assert not _hides_its_back(inverted)


class TestANeedleMeshRendersClean:
    def test_a_tube_of_full_height_slivers_has_smooth_walls(self, tmp_path):
        from backend.app.services.stl_thumbnail import generate_stl_thumbnail

        source = tmp_path / "tube.stl"
        _tube().export(source)
        picture = generate_stl_thumbnail(source, tmp_path)
        assert picture is not None
        flawed, inside = _rough_pixels(picture)
        assert inside > 2000, "the tube fills the middle of the picture"
        # measured before the change: 0.98 of this tube's pixels were seams or streaks (0.35 and 0.54 on the two
        # real CAD exports that prompted it); what is left afterwards is the tube's own inner edge
        assert flawed / inside < 0.10, f"{flawed} of {inside} wall pixels are streaks or seams"

    def test_an_ordinary_mesh_looks_as_it_did(self, tmp_path):
        """A cube has no needles: its three visible sides keep their three tones."""
        from backend.app.services.stl_thumbnail import generate_stl_thumbnail

        source = tmp_path / "cube.stl"
        trimesh.creation.box(extents=(20, 20, 20)).export(source)
        picture = generate_stl_thumbnail(source, tmp_path)
        rgba = np.asarray(Image.open(picture).convert("RGBA")).astype(int)
        tones = rgba[rgba[:, :, 3] == 255][:, 1]
        counts = np.bincount(tones, minlength=256)
        assert (counts > 0.05 * len(tones)).sum() == 3, "three flat sides, three tones, nothing else large"
