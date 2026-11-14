"""STL Thumbnail Generation Service.

Generates thumbnail images from STL files using trimesh and matplotlib.
"""

import logging
import os
import sys
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# Matplotlib's font_manager emits one INFO line per font on first import while
# it builds its cache, including a noisy "Failed to extract font properties from
# NotoColorEmoji.ttf" for the COLR/COLR1 emoji format it doesn't support. These
# are not actionable — demote to WARNING so real font issues still surface but
# the first STL upload doesn't produce a multi-line matplotlib preamble in the
# journal (#1759).
logging.getLogger("matplotlib.font_manager").setLevel(logging.WARNING)


def _configure_matplotlib_cache() -> None:
    """Point matplotlib's config/cache directory at a writable persistent path.

    Without this, matplotlib falls back to ``/tmp/matplotlib-XXXXXX`` whenever
    ``$HOME/.config/matplotlib`` isn't writable — the case under BamDude's
    container / systemd-service deployments where ``$HOME`` is non-writable. The
    fallback emits a WARNING on every cold start AND loses the font cache on host
    reboot, so font_manager rebuilds it every time → another batch of INFO lines.

    Setting ``MPLCONFIGDIR`` to ``settings.base_dir / .cache / matplotlib``
    eliminates both: the warning never fires, and the cache survives across
    restarts so the per-font scan only runs once per deployment. Idempotent —
    respects an externally-set ``MPLCONFIGDIR`` if the operator chose their own.
    """
    if os.environ.get("MPLCONFIGDIR"):
        return
    try:
        from backend.app.core.config import settings

        cache_dir = Path(settings.base_dir) / ".cache" / "matplotlib"
        cache_dir.mkdir(parents=True, exist_ok=True)
        os.environ["MPLCONFIGDIR"] = str(cache_dir)
    except Exception as exc:
        # Best-effort. If settings isn't importable or the mkdir fails (read-only
        # FS, permission denied), let matplotlib fall back to /tmp with its
        # built-in warning — same as today's behaviour, no worse.
        logger.debug("Could not configure MPLCONFIGDIR: %s", exc)


# Bambu green color for rendering
BAMBU_GREEN = "#00AE42"

# The camera: an isometric front-quarter that shows the front face, the right
# side and the top at once — the standard CAD-preview pose. plate_thumbnail
# reads these two rather than keeping its own: "a plate card and a library
# thumbnail of the same model look alike" is the reason the renderers share a
# look, and a second copy of an angle is how that silently stops being true.
VIEW_ELEV_DEG = 30
VIEW_AZIM_DEG = -60

# The light, chosen AGAINST the camera above — the two are a pair (upstream
# ed856779, #2816). matplotlib's light direction for (az, alt) is
# ``[cos(90-az)cos(alt), sin(90-az)cos(alt), sin(alt)]`` and the camera set by
# ``view_init(elev, azim)`` sits at ``[cos(elev)cos(azim), cos(elev)sin(azim),
# sin(elev)]``; the dot product of the two must be POSITIVE or the light is
# behind the model. The 315 shipped before scored -0.24 against this camera and
# lit both visible sides to the identical 0.475 — a cube with no front edge. At
# 240 it is +0.35 and the light comes from the viewer's upper left: left side
# 0.77, right side 0.44, top 0.90. Moving either constant without the other puts
# the light back behind the model; ``test_light_gives_the_two_visible_faces_
# different_shades`` holds the pair.
LIGHT_AZIMUTH_DEG = 240
LIGHT_ALTITUDE_DEG = 45

# Maximum vertices before simplification
MAX_VERTICES = 100000

# matplotlib has no depth buffer: it sorts triangles by ONE average depth and
# draws them back to front. That holds while triangles are small; a CAD export is
# often made of needles — a cylinder's wall as slivers running its whole height, a
# flat cap as wedges from the centre to the rim — and one depth for a triangle
# that spans the model sorts it wrongly against its neighbours: wedges show
# through, the far wall is painted over the near one. A triangle whose longest
# edge exceeds this share of the model's size is split before it is drawn.
LONG_EDGE_SHARE = 0.05

# The view is a mild perspective, so a face the parallel rule calls edge-on can
# still show a sliver of itself: faces turned away by less than this (the cosine
# of the angle past edge-on) are kept.
BACK_FACE_SLACK = 0.12

# Minimum STL file size that could possibly contain a usable mesh:
# - Binary STL with one triangle: 80B header + 4B count + 50B triangle = 134B
# - ASCII STL with one triangle: header + "facet ... endfacet" + footer ≈ 150B
# Files below this are stubs / placeholders / corrupted; trimesh would return an
# empty mesh anyway. Pre-skipping at the call sites suppresses the warning storm
# bulk-uploaded ZIPs of small test STLs used to produce (#1820).
MIN_USABLE_STL_BYTES = 200


def _repair_winding(mesh, trimesh, label: str) -> None:
    """Make every face wind the same way, and wind it OUTWARD, before shading.

    matplotlib derives its normals from vertex ORDER, so a triangle wound the
    wrong way shades as though it faced away and the model comes out patchy —
    camouflage rather than a surface. ``trimesh.load(force="mesh")`` does not
    repair winding; this does (upstream ed856779).

    ``trimesh.repair.fix_winding`` and not ``mesh.fix_normals()``: the latter
    reaches ``body_count`` -> ``scipy.csgraph``, and this renderer should not
    need a graph engine beyond the networkx fix_winding already uses.

    Three steps, because each one leaves something for the next:

    * ``fix_winding`` makes the winding agree but is free to settle on either
      orientation, and on a half-inverted sphere it picks INWARD.
    * ``fix_inversion`` corrects that off the sign of the volume, but only for a
      WATERTIGHT mesh — a volume measured across holes says nothing.
    * Which leaves the common case, since a mesh with broken winding is usually
      not watertight either: with no usable volume, decide by whether the faces
      point away from the centroid.

    Gated on ``is_winding_consistent``: the check is tens of ms where the repair
    is seconds on a large mesh, so only meshes that would render wrong pay.
    Shared with plate_thumbnail so the two renderers cannot drift.
    """
    import numpy as np

    if len(mesh.faces) == 0 or mesh.is_winding_consistent:
        return

    logger.debug("Repairing inconsistent winding before render: %s", label)
    trimesh.repair.fix_winding(mesh)
    trimesh.repair.fix_inversion(mesh)
    if mesh.is_watertight:
        return

    outward = mesh.triangles.mean(axis=1) - mesh.vertices.mean(axis=0)
    if float(np.einsum("ij,ij->i", mesh.face_normals, outward).sum()) < 0:
        logger.debug("Winding settled inward on a non-watertight mesh, inverting: %s", label)
        mesh.invert()


def _camera_direction():
    """Unit vector from the model towards the camera ``view_init`` places."""
    import numpy as np

    elev, azim = np.radians(VIEW_ELEV_DEG), np.radians(VIEW_AZIM_DEG)
    return np.array([np.cos(elev) * np.cos(azim), np.cos(elev) * np.sin(azim), np.sin(elev)])


def _hides_its_back(mesh) -> bool:
    """Whether the faces turned away from the camera can be left out: only for a closed mesh wound outward.

    On such a mesh they are never visible, and drawing them is how the inside of a thin wall ends up painted
    over its outside (the two surfaces are a wall's thickness apart, closer than the depth sort can tell).
    An open mesh may be showing its inside through the opening, and a mesh wound inward would lose the very
    faces the viewer looks at.
    """
    try:
        return bool(mesh.is_watertight and mesh.is_winding_consistent and mesh.volume > 0)
    except Exception:  # a mesh trimesh cannot judge is drawn whole
        return False


def _facing_camera(triangles):
    """The triangles a closed mesh shows to the camera (see ``_hides_its_back``); the others are dropped."""
    import numpy as np

    if len(triangles) == 0:
        return triangles
    normals = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    lengths = np.linalg.norm(normals, axis=1)
    facing = normals @ _camera_direction() > -BACK_FACE_SLACK * lengths
    return triangles[facing] if facing.any() else triangles


def _split_long_triangles(triangles, max_edge: float, max_faces: int):
    """Halve every triangle whose longest edge exceeds ``max_edge``, until none does or the budget is spent.

    Only the long ones: an ordinary mesh comes back untouched (the same array). A split bisects the longest
    edge, so the two halves keep the triangle's winding and plane — the shading does not change, only the
    depth each piece is sorted by. The pieces of neighbours no longer share vertices, which a renderer that
    draws flat triangles does not care about.
    """
    import numpy as np

    for _ in range(16):
        if len(triangles) == 0:
            return triangles
        edges = np.linalg.norm(triangles - np.roll(triangles, -1, axis=1), axis=2)  # 0-1, 1-2, 2-0
        long = edges.max(axis=1) > max_edge
        count = int(long.sum())
        if count == 0 or len(triangles) + count > max_faces:
            return triangles
        chosen = triangles[long]
        first = edges[long].argmax(axis=1)
        row = np.arange(count)
        a, b, c = chosen[row, first], chosen[row, (first + 1) % 3], chosen[row, (first + 2) % 3]
        middle = (a + b) / 2
        triangles = np.concatenate(
            [triangles[~long], np.stack([a, middle, c], axis=1), np.stack([middle, b, c], axis=1)]
        )
    return triangles


def _shade_kwargs(poly3d, LightSource) -> dict:
    """``shade=True`` and its light, or nothing when the mesh cannot be shaded.

    matplotlib's ``_shade_colors`` falls back, for a mesh whose every face normal
    is degenerate, to returning the colour argument unchanged — and for a colour
    STRING that is a 0-d array, on which ``to_rgba_array`` raises ``TypeError:
    len() of unsized object``. Stub and truncated STLs, and 3MFs with an empty
    ``<triangles/>``, reach here; deciding up front keeps their flat render a
    real outcome instead of a failure with a traceback. Identical to
    matplotlib's own test: a cross product finite and non-zero for at least one
    face.
    """
    import numpy as np

    if len(poly3d) == 0:
        return {}
    tri = np.asarray(poly3d, dtype=float)
    normals = np.cross(tri[:, 0] - tri[:, 1], tri[:, 1] - tri[:, 2])
    lengths = np.linalg.norm(normals, axis=1)
    if not bool(np.any(np.isfinite(lengths) & (lengths > 0))):
        return {}
    return {
        "shade": True,
        "lightsource": LightSource(azdeg=LIGHT_AZIMUTH_DEG, altdeg=LIGHT_ALTITUDE_DEG),
    }


def generate_stl_thumbnail(
    stl_path: Path,
    thumbnails_dir: Path,
    size: int = 256,
) -> str | None:
    try:
        return _generate_stl_thumbnail(stl_path, thumbnails_dir, size)
    finally:
        # This renderer runs in a disposable child; also close failed figures.
        if pyplot := sys.modules.get("matplotlib.pyplot"):
            pyplot.close("all")


def _generate_stl_thumbnail(stl_path: Path, thumbnails_dir: Path, size: int) -> str | None:
    """Generate a thumbnail image from an STL file.

    Args:
        stl_path: Path to the STL file
        thumbnails_dir: Directory to save the thumbnail
        size: Thumbnail size in pixels (default 256x256)

    Returns:
        Path to the generated thumbnail, or None on failure
    """
    # Callers historically pass either Path or str; coerce so the
    # ``thumbnails_dir / thumb_filename`` join at the end of this
    # function can't fail with the str-divided-by-str TypeError
    # (upstream Bambuddy #1299).
    stl_path = Path(stl_path)
    thumbnails_dir = Path(thumbnails_dir)

    try:
        # Must precede the matplotlib import — MPLCONFIGDIR is read at
        # matplotlib import time, not on subsequent attribute access.
        _configure_matplotlib_cache()

        import matplotlib
        import trimesh

        # Use Agg backend for headless rendering
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib.colors import LightSource
        from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
        from mpl_toolkits.mplot3d.art3d import Poly3DCollection

        # Load the STL file
        mesh = trimesh.load(str(stl_path), force="mesh")

        if mesh is None or not hasattr(mesh, "vertices") or len(mesh.vertices) == 0:
            # Demoted from warning to debug: this is a per-file content
            # observation (the STL is empty / stub / corrupted), not an
            # actionable error. The caller proceeds correctly with no thumbnail.
            # The call sites also pre-skip files below MIN_USABLE_STL_BYTES so
            # the common stub-STL case never gets this far — this branch now
            # catches only the rare "large enough but trimesh still can't parse
            # it" case (#1820).
            logger.debug("Failed to load STL or empty mesh: %s", stl_path)
            return None

        # Simplify large meshes for performance
        from backend.app.services.preview_protocol import FACE_LIMIT

        if len(mesh.vertices) > MAX_VERTICES or len(mesh.faces) > FACE_LIMIT:
            logger.info("Simplifying mesh from %s vertices", len(mesh.vertices))
            try:
                # Calculate reduction ratio (0-1 range)
                # e.g., 124633 vertices -> 100000 means keep ~80%, so reduce by ~20%
                keep_ratio = MAX_VERTICES / len(mesh.vertices)
                mesh = mesh.simplify_quadric_decimation(face_count=min(FACE_LIMIT, int(len(mesh.faces) * keep_ratio)))
                logger.info("Simplified mesh to %s vertices", len(mesh.vertices))
            except Exception as e:
                logger.warning("Mesh simplification failed: %s", e)
            if len(mesh.faces) > FACE_LIMIT:
                return None

        # Wind every face the same way, and outward, or the shading turns the
        # model into camouflage. Before the vertices below are read: ``poly3d``
        # is indexed by ``mesh.faces``, so a repair that ever moved a vertex
        # would leave the two out of step.
        try:
            _repair_winding(mesh, trimesh, str(stl_path))
        except Exception as e:  # best-effort: a flat render beats no thumbnail
            logger.debug("Winding repair skipped (%s): %s", e, stl_path)

        # Get mesh bounds and center it
        vertices = mesh.vertices
        bounds_min = vertices.min(axis=0)
        bounds_max = vertices.max(axis=0)
        center = (bounds_min + bounds_max) / 2
        vertices_centered = vertices - center

        # Scale to fit in view
        max_extent = (bounds_max - bounds_min).max()
        if max_extent > 0:
            scale = 1.0 / max_extent
            vertices_scaled = vertices_centered * scale
        else:
            vertices_scaled = vertices_centered

        # Render at 3× target resolution so the post-render alpha-bbox crop
        # + Lanczos downscale produces clean antialiased edges. Internal
        # render is ``size * RENDER_SCALE`` pixels per side; after cropping
        # transparent margins around the model and resizing to fit ``size``
        # on the longest dim, edges are smooth and the model fills the
        # output PNG instead of leaving matplotlib's reserved-but-empty
        # 3D-axes margins around it.
        RENDER_SCALE = 3
        render_dpi = 100 * RENDER_SCALE
        fig = plt.figure(figsize=(size / 100, size / 100), dpi=render_dpi)
        fig.patch.set_alpha(0)

        ax = fig.add_subplot(111, projection="3d")
        ax.set_facecolor("none")
        # Hide the 3D pane backgrounds (the gray "walls" matplotlib draws
        # behind axes) so the transparent fig background shows through —
        # set_axis_off() below stops the tick labels from drawing but the
        # panes themselves are separate artists.
        for pane in (ax.xaxis.pane, ax.yaxis.pane, ax.zaxis.pane):
            pane.set_visible(False)

        # Create polygon collection from mesh faces. ``shade=True`` makes
        # matplotlib compute per-face normals and apply Lambertian shading
        # against the light — without it Poly3DCollection ships every face the
        # flat ``facecolors`` value and the model looks like a 2D silhouette
        # regardless of geometry. The light is the one chosen against the
        # camera (``LIGHT_AZIMUTH_DEG``); ``_shade_kwargs`` drops it for a mesh
        # matplotlib cannot shade.
        #
        # Note on edge handling: when ``shade=True`` matplotlib runs the
        # shading pipeline on edgecolors too — passing ``'none'`` raises
        # ``ValueError: operands could not be broadcast (4,1) (0,4)`` from
        # the empty-color-array path, and the special ``'face'`` keyword
        # isn't recognised by ``_shade_colors``. Workaround: pass an
        # explicit colour matching ``facecolors`` and rely on
        # ``linewidths=0`` to keep the wireframe invisible.
        #
        # Indexed with the face array rather than built as a list of lists:
        # same data, but shading walks it to generate normals, and on an
        # 82k-face mesh the list form costs ~0.19 s against ~0.007 s.
        poly3d = vertices_scaled[mesh.faces]

        # A mesh of needles would come out torn (see ``LONG_EDGE_SHARE``): leave
        # out what a closed mesh cannot show, then split what is too long to be
        # sorted by one depth. The model is scaled to 1.0 on its longest side.
        if _hides_its_back(mesh):
            poly3d = _facing_camera(poly3d)
        poly3d = _split_long_triangles(poly3d, LONG_EDGE_SHARE, FACE_LIMIT)

        # ``antialiaseds=False``: matplotlib smooths the edge of every polygon
        # on its own, which leaves a see-through hairline between two that share
        # an edge — on a wall of thousands of slivers the hairlines ARE the
        # picture. The render is three times the final size and scaled down with
        # Lanczos below, so the silhouette is still smooth.
        collection = Poly3DCollection(
            poly3d,
            facecolors=BAMBU_GREEN,
            edgecolors=BAMBU_GREEN,
            linewidths=0,
            alpha=1.0,
            antialiaseds=False,
            **_shade_kwargs(poly3d, LightSource),
        )
        ax.add_collection3d(collection)
        # Without this matplotlib uses its automatic z-order computation
        # which sometimes draws far faces over near ones at certain camera
        # angles. Explicit ``False`` falls back to insertion order, which
        # for shaded models reads correctly.
        ax.computed_zorder = False

        # Tight axis limits — vertices are scaled to fit in [-0.5, 0.5]
        # along the longest dimension above, so matching the view box to
        # that range maxes the model size on screen. The 5% slack
        # (±0.525) prevents corner clipping when the model is rotated
        # and its bounding-box diagonal pokes slightly past axis-aligned
        # bounds on certain camera angles.
        ax.set_xlim(-0.525, 0.525)
        ax.set_ylim(-0.525, 0.525)
        ax.set_zlim(-0.525, 0.525)

        # Isometric front-quarter — see ``VIEW_ELEV_DEG``. Better than the
        # previous (elev=25, azim=45), which buried the front face.
        ax.view_init(elev=VIEW_ELEV_DEG, azim=VIEW_AZIM_DEG)

        # Remove axes and grid
        ax.set_axis_off()
        ax.grid(False)

        # Remove margins
        plt.subplots_adjust(left=0, right=1, top=1, bottom=0)

        # Save thumbnail
        thumb_filename = f"{uuid.uuid4().hex}.png"
        thumb_path = (
            thumbnails_dir / thumb_filename
        )  # SEC-PATH-OK: thumb_filename = uuid4().hex + a fixed .png extension (server-generated)

        fig.savefig(
            thumb_path,
            format="png",
            transparent=True,
            edgecolor="none",
            bbox_inches="tight",
            pad_inches=0,
            dpi=render_dpi,
        )
        plt.close(fig)

        # Post-process: matplotlib's 3D ``Axes3D`` reserves layout space
        # for axis labels even when ``set_axis_off()`` is called, so
        # ``bbox_inches='tight'`` alone leaves transparent margins around
        # the model. Pipeline:
        #   1. Open the supersampled render.
        #   2. ``Image.getbbox()`` returns the bbox of non-zero alpha
        #      pixels — i.e. the actual model silhouette.
        #   3. Crop with a small antialias-edge slack.
        #   4. Lanczos-downscale to fit ``size`` on the longest side
        #      (preserving aspect ratio — a tall narrow model lands as
        #      e.g. 256×320 instead of forcing a square).
        # The supersample → crop → Lanczos chain produces noticeably
        # smoother edges than rendering at the final resolution directly.
        try:
            from PIL import Image

            with Image.open(thumb_path) as img:
                if img.mode != "RGBA":
                    img = img.convert("RGBA")
                bbox = img.getbbox()
                if bbox is not None:
                    # Padding scales with render resolution so the relative
                    # margin stays the same after downscale.
                    pad = 4 * RENDER_SCALE
                    left = max(bbox[0] - pad, 0)
                    top = max(bbox[1] - pad, 0)
                    right = min(bbox[2] + pad, img.width)
                    bottom = min(bbox[3] + pad, img.height)
                    cropped = img.crop((left, top, right, bottom))

                    # Downscale to target size, longest-side-fit, preserving
                    # aspect ratio. Lanczos for high-quality reduction.
                    max_dim = max(cropped.width, cropped.height)
                    if max_dim > size:
                        scale = size / max_dim
                        new_w = max(1, round(cropped.width * scale))
                        new_h = max(1, round(cropped.height * scale))
                        cropped = cropped.resize((new_w, new_h), Image.Resampling.LANCZOS)
                    cropped.save(thumb_path, format="PNG", optimize=True)
        except Exception as e:
            # Best-effort — if PIL crop fails, the un-cropped image still
            # works fine, just with slightly more transparent margin and
            # at the supersampled resolution.
            logger.debug("PIL post-process failed for %s: %s", thumb_path, e)

        logger.info("Generated STL thumbnail: %s", thumb_path)
        return str(thumb_path)

    except ImportError as e:
        logger.warning("STL thumbnail generation unavailable (missing dependencies): %s", e)
        return None
    except Exception as e:
        # Log the traceback, not just the message: a bare
        # "unsupported operand type(s) for /: 'str' and 'str'" gives no clue
        # which line failed, and the fault is data-/environment-specific
        # enough that it can't be reproduced from a clean STL — the traceback
        # in the next support bundle is what pinpoints it (#1480).
        logger.warning("Failed to generate STL thumbnail for %s: %s", stl_path, e, exc_info=True)
        return None
