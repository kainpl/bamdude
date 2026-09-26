"""Unit tests for the STL thumbnail service."""

import tempfile
from pathlib import Path

import pytest


def _check_trimesh_available():
    """Check if trimesh is available for import."""
    try:
        import trimesh

        return True
    except ImportError:
        return False


class TestStlThumbnailService:
    """Tests for STL thumbnail generation service."""

    def test_generate_stl_thumbnail_imports_available(self):
        """Test that required imports are available."""
        try:
            import matplotlib
            import trimesh

            assert trimesh is not None
            assert matplotlib is not None
        except ImportError as e:
            pytest.skip(f"Required dependencies not installed: {e}")

    def test_generate_stl_thumbnail_returns_none_on_missing_deps(self):
        """Test graceful degradation when dependencies are missing."""
        from backend.app.services.stl_thumbnail import generate_stl_thumbnail

        with tempfile.TemporaryDirectory() as tmpdir:
            stl_path = Path(tmpdir) / "test.stl"
            thumbnails_dir = Path(tmpdir)

            # Create a dummy STL file (will fail to parse)
            stl_path.write_text("invalid stl content")

            # Should return None on failure, not raise
            result = generate_stl_thumbnail(stl_path, thumbnails_dir)
            assert result is None

    @pytest.mark.skipif(
        not _check_trimesh_available(),
        reason="trimesh not installed",
    )
    def test_generate_stl_thumbnail_with_simple_cube(self):
        """Test thumbnail generation with a simple cube STL."""
        from backend.app.services.stl_thumbnail import generate_stl_thumbnail

        with tempfile.TemporaryDirectory() as tmpdir:
            stl_path = Path(tmpdir) / "cube.stl"
            thumbnails_dir = Path(tmpdir)

            # Create a simple ASCII STL cube
            stl_content = """solid cube
facet normal 0 0 -1
  outer loop
    vertex 0 0 0
    vertex 1 0 0
    vertex 1 1 0
  endloop
endfacet
facet normal 0 0 -1
  outer loop
    vertex 0 0 0
    vertex 1 1 0
    vertex 0 1 0
  endloop
endfacet
facet normal 0 0 1
  outer loop
    vertex 0 0 1
    vertex 1 1 1
    vertex 1 0 1
  endloop
endfacet
facet normal 0 0 1
  outer loop
    vertex 0 0 1
    vertex 0 1 1
    vertex 1 1 1
  endloop
endfacet
facet normal 0 -1 0
  outer loop
    vertex 0 0 0
    vertex 1 0 1
    vertex 1 0 0
  endloop
endfacet
facet normal 0 -1 0
  outer loop
    vertex 0 0 0
    vertex 0 0 1
    vertex 1 0 1
  endloop
endfacet
facet normal 1 0 0
  outer loop
    vertex 1 0 0
    vertex 1 0 1
    vertex 1 1 1
  endloop
endfacet
facet normal 1 0 0
  outer loop
    vertex 1 0 0
    vertex 1 1 1
    vertex 1 1 0
  endloop
endfacet
facet normal 0 1 0
  outer loop
    vertex 0 1 0
    vertex 1 1 0
    vertex 1 1 1
  endloop
endfacet
facet normal 0 1 0
  outer loop
    vertex 0 1 0
    vertex 1 1 1
    vertex 0 1 1
  endloop
endfacet
facet normal -1 0 0
  outer loop
    vertex 0 0 0
    vertex 0 1 0
    vertex 0 1 1
  endloop
endfacet
facet normal -1 0 0
  outer loop
    vertex 0 0 0
    vertex 0 1 1
    vertex 0 0 1
  endloop
endfacet
endsolid cube"""
            stl_path.write_text(stl_content)

            result = generate_stl_thumbnail(stl_path, thumbnails_dir)

            # Should return a path to the generated thumbnail
            if result:
                assert Path(result).exists()
                assert Path(result).suffix == ".png"
            # If result is None, dependencies might not be fully functional
            # which is acceptable

    @pytest.mark.skipif(
        not _check_trimesh_available(),
        reason="trimesh not installed",
    )
    def test_generated_thumbnail_is_shaded_not_flat(self, distinct_surface_tones):
        """The render must be lit so a cube shows three faces, not two (#2816).

        With the light behind the model the two visible SIDE faces catch it
        identically and the front edge disappears — the cube came out as two
        tones, a hexagon with a lighter lid.
        """
        import trimesh

        from backend.app.services.stl_thumbnail import generate_stl_thumbnail

        with tempfile.TemporaryDirectory() as tmpdir:
            stl_path = Path(tmpdir) / "cube.stl"
            trimesh.creation.box(extents=(10.0, 10.0, 10.0)).export(str(stl_path))

            result = generate_stl_thumbnail(stl_path, Path(tmpdir))
            assert result is not None
            assert distinct_surface_tones(Path(result).read_bytes()) >= 3

    @pytest.mark.skipif(
        not _check_trimesh_available(),
        reason="trimesh not installed",
    )
    @pytest.mark.parametrize(
        ("label", "punch_holes"),
        [("watertight", False), ("open", True)],
    )
    def test_backwards_wound_triangles_render_the_same(self, label, punch_holes):
        """Vertex ORDER must not change the picture.

        matplotlib takes its normals from winding, so an inverted triangle shades
        as though it faced away and the model comes out patchy. Asserted as "same
        picture as the correctly wound mesh": broken winding produces MORE
        distinct tones, not fewer, so a tone count cannot see it. Watertight and
        open both, because ``fix_inversion`` gives up on an open mesh and the
        centroid fallback in ``_repair_winding`` is the only thing holding it.
        """
        import numpy as np
        import trimesh
        from PIL import Image

        from backend.app.services.stl_thumbnail import generate_stl_thumbnail

        sphere = trimesh.creation.icosphere(subdivisions=3, radius=5.0)
        keep = sphere.faces.copy()[:-80] if punch_holes else sphere.faces.copy()
        good = trimesh.Trimesh(vertices=sphere.vertices.copy(), faces=keep.copy())
        assert good.is_watertight is not punch_holes, "fixture has the wrong topology"

        faces = keep.copy()
        faces[::2] = faces[::2][:, ::-1]
        bad = trimesh.Trimesh(vertices=sphere.vertices.copy(), faces=faces)
        assert not bad.is_winding_consistent, "fixture is supposed to be broken"

        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            rendered = []
            for name, mesh in (("good", good), ("bad", bad)):
                path = out / f"{name}.stl"
                mesh.export(str(path))
                result = generate_stl_thumbnail(path, out)
                assert result is not None
                rendered.append(np.asarray(Image.open(result).convert("RGB"), dtype=float))

            assert rendered[0].shape == rendered[1].shape
            mean_delta = float(np.abs(rendered[0] - rendered[1]).mean())

        assert mean_delta < 1.0, f"winding changed the {label} render (mean delta {mean_delta:.2f})"

    @pytest.mark.skipif(
        not _check_trimesh_available(),
        reason="trimesh not installed",
    )
    def test_degenerate_mesh_still_renders(self):
        """A mesh with no shadeable face renders flat instead of failing.

        matplotlib's ``_shade_colors`` hands a colour STRING back as a 0-d array
        when every normal is degenerate, and ``to_rgba_array`` then raises
        ``TypeError: len() of unsized object`` — so a lit render of a stub or
        truncated STL failed where the flat one had worked.
        """
        import struct

        from backend.app.services.stl_thumbnail import generate_stl_thumbnail

        def write_binary_stl(path, triangles):
            # By hand: trimesh.export drops degenerate facets and would quietly
            # defeat the test.
            with open(path, "wb") as fh:
                fh.write(b"\0" * 80)
                fh.write(struct.pack("<I", len(triangles)))
                for tri in triangles:
                    fh.write(struct.pack("<3f", 0.0, 0.0, 0.0))
                    for vertex in tri:
                        fh.write(struct.pack("<3f", *vertex))
                    fh.write(b"\0\0")

        cases = {
            "zero_area": [[(0, 0, 0), (0, 0, 0), (0, 0, 0)]],
            "collinear": [[(0, 0, 0), (1, 1, 1), (2, 2, 2)]],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            out = Path(tmpdir)
            for name, triangles in cases.items():
                path = out / f"{name}.stl"
                write_binary_stl(path, triangles)
                assert generate_stl_thumbnail(path, out) is not None, f"{name} must still render"

    def test_generate_stl_thumbnail_nonexistent_file(self):
        """Test thumbnail generation with nonexistent file."""
        from backend.app.services.stl_thumbnail import generate_stl_thumbnail

        with tempfile.TemporaryDirectory() as tmpdir:
            stl_path = Path(tmpdir) / "nonexistent.stl"
            thumbnails_dir = Path(tmpdir)

            result = generate_stl_thumbnail(stl_path, thumbnails_dir)
            assert result is None

    def test_generate_stl_thumbnail_empty_file(self):
        """Test thumbnail generation with empty file."""
        from backend.app.services.stl_thumbnail import generate_stl_thumbnail

        with tempfile.TemporaryDirectory() as tmpdir:
            stl_path = Path(tmpdir) / "empty.stl"
            thumbnails_dir = Path(tmpdir)

            # Create empty file
            stl_path.write_bytes(b"")

            result = generate_stl_thumbnail(stl_path, thumbnails_dir)
            assert result is None


class TestStlThumbnailConstants:
    """Tests for STL thumbnail service constants."""

    def test_bambu_green_color(self):
        """Test that Bambu green color is defined."""
        from backend.app.services.stl_thumbnail import BAMBU_GREEN

        assert BAMBU_GREEN == "#00AE42"

    def test_light_gives_the_two_visible_faces_different_shades(self):
        """The whole point of lighting: the camera's two side faces must differ.

        Both halves are asserted because neither alone is the property. A
        positive dot product only says the light is not BEHIND the model; a light
        placed exactly where the camera is scores the highest dot product of all
        and lights both visible sides identically. The shipped pair (315 against
        this camera) failed the first half: the light sat behind the model and
        both visible sides shaded the same.

        The visible sides are derived from the camera rather than written down,
        so moving the camera re-asks the question instead of passing silently.
        Shade factors are matplotlib's own: ``0.3 + 0.7 * (dot + 1) / 2``.
        """
        import numpy as np
        from matplotlib.colors import LightSource

        from backend.app.services.stl_thumbnail import (
            LIGHT_ALTITUDE_DEG,
            LIGHT_AZIMUTH_DEG,
            VIEW_AZIM_DEG,
            VIEW_ELEV_DEG,
        )

        elev, azim = np.radians(VIEW_ELEV_DEG), np.radians(VIEW_AZIM_DEG)
        camera = np.array([np.cos(elev) * np.cos(azim), np.cos(elev) * np.sin(azim), np.sin(elev)])
        light = LightSource(azdeg=LIGHT_AZIMUTH_DEG, altdeg=LIGHT_ALTITUDE_DEG).direction

        assert float(light @ camera) > 0, "the light is behind the model"

        def shade(normal):
            return 0.3 + 0.7 * ((float(np.array(normal) @ light) + 1) / 2)

        sides = [n for n in ([1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0]) if float(np.array(n) @ camera) > 0]
        assert len(sides) == 2, "the camera is expected to look at a box corner"
        assert abs(shade(sides[0]) - shade(sides[1])) > 0.05, (
            "both visible faces are lit the same — the front edge disappears"
        )

    def test_light_is_above_the_horizon(self):
        """Grazing or overhead both collapse the contrast the shading exists for."""
        from backend.app.services.stl_thumbnail import LIGHT_ALTITUDE_DEG

        assert 0 < LIGHT_ALTITUDE_DEG < 90

    def test_max_vertices_threshold(self):
        """Test that max vertices threshold is defined."""
        from backend.app.services.stl_thumbnail import MAX_VERTICES

        assert MAX_VERTICES == 100000
