"""Add ``extra_colors`` + ``effect_type`` to ``color_catalog``.

PR #1154 ("multi-colour gradients, transparency, visual effects") shipped
the columns on the ``spool`` table back in BamDude's m026, but the
``color_catalog`` table — which holds the user-curated swatch presets used
in the Spool Form's catalog picker — never got the same fields. That meant
a catalog entry could only carry a flat hex; you couldn't curate a "PLA
Galaxy" preset with gradient stops + sparkle effect for one-click apply.

Upstream Bambuddy #1340 / commit ``b51ef334`` plumbed ``extra_colors`` and
``effect_type`` through the FE catalog-swatch click → ``selectColor``
handler so picking such an entry writes the full preset look onto the
spool, not just hex + name. The FE wiring landed in BamDude's D.15
(commit ``b1827d2``), but was effectively no-op until these columns
exist on the catalog row to be read in the first place.

Same column shape as the corresponding ``spool`` columns: ``extra_colors``
holds the comma-joined canonical lowercase hex tokens (≤8 stops),
``effect_type`` holds one of the canonical effect names (matte / silk /
sparkle / wood / marble / glow / dual-color / tri-color / multicolor /
gradient — same `ALLOWED_EFFECT_TYPES` set the spool form uses).

Both default NULL — existing catalog entries render as flat hex
unchanged.

Upstream Bambuddy #1154 / commit ``a34beaa5`` (model + migration).
"""

from backend.app.migrations.helpers import add_column

version = 76
name = "color_catalog_extra_colors"


async def upgrade(conn):
    await add_column(conn, "color_catalog", "extra_colors VARCHAR(255)")
    await add_column(conn, "color_catalog", "effect_type VARCHAR(20)")
