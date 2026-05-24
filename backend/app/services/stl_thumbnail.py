"""STL Thumbnail Generation Service.

Generates thumbnail images from STL files using trimesh and matplotlib.
"""

import logging
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

# Bambu green color for rendering
BAMBU_GREEN = "#00AE42"

# Maximum vertices before simplification
MAX_VERTICES = 100000


def generate_stl_thumbnail(
    stl_path: Path,
    thumbnails_dir: Path,
    size: int = 256,
) -> str | None:
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
            logger.warning("Failed to load STL or empty mesh: %s", stl_path)
            return None

        # Simplify large meshes for performance
        if len(mesh.vertices) > MAX_VERTICES:
            logger.info("Simplifying mesh from %s vertices", len(mesh.vertices))
            try:
                # Calculate reduction ratio (0-1 range)
                # e.g., 124633 vertices -> 100000 means keep ~80%, so reduce by ~20%
                keep_ratio = MAX_VERTICES / len(mesh.vertices)
                target_reduction = 1.0 - keep_ratio
                # Clamp to valid range (0.01 to 0.99)
                target_reduction = max(0.01, min(0.99, target_reduction))
                mesh = mesh.simplify_quadric_decimation(target_reduction)
                logger.info("Simplified mesh to %s vertices", len(mesh.vertices))
            except Exception as e:
                logger.warning("Mesh simplification failed, using original: %s", e)

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
        # against the configured ``LightSource`` — without it Poly3DCollection
        # ships every face the flat ``facecolors`` value and the model looks
        # like a 2D silhouette regardless of geometry. LightSource azimuth
        # 315° + altitude 45° is the standard "sun from upper-left" rendering
        # convention — matches how Bambu Studio + most 3D CAD tools light
        # their default scene, so the visual cue lines up with what operators
        # see in slicer previews.
        #
        # Note on edge handling: when ``shade=True`` matplotlib runs the
        # shading pipeline on edgecolors too — passing ``'none'`` raises
        # ``ValueError: operands could not be broadcast (4,1) (0,4)`` from
        # the empty-color-array path, and the special ``'face'`` keyword
        # isn't recognised by ``_shade_colors``. Workaround: pass an
        # explicit colour matching ``facecolors`` and rely on
        # ``linewidths=0`` to keep the wireframe invisible.
        faces = mesh.faces
        poly3d = [[vertices_scaled[vertex] for vertex in face] for face in faces]

        ls = LightSource(azdeg=315, altdeg=45)
        collection = Poly3DCollection(
            poly3d,
            facecolors=BAMBU_GREEN,
            edgecolors=BAMBU_GREEN,
            linewidths=0,
            alpha=1.0,
            shade=True,
            lightsource=ls,
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

        # Isometric front-quarter — shows the front face, right side, and
        # top simultaneously. Standard CAD-preview pose; better than the
        # previous (elev=25, azim=45) which buried the front face.
        ax.view_init(elev=30, azim=-60)

        # Remove axes and grid
        ax.set_axis_off()
        ax.grid(False)

        # Remove margins
        plt.subplots_adjust(left=0, right=1, top=1, bottom=0)

        # Save thumbnail
        thumb_filename = f"{uuid.uuid4().hex}.png"
        thumb_path = thumbnails_dir / thumb_filename

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
