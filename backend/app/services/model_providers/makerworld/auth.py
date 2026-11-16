"""MakerWorld credential handling.

MakerWorld downloads run on the same Bambu Cloud bearer token as the rest of
the Bambu cloud integration — there is no separate MakerWorld OAuth flow. This
module is the single seam where the MakerWorld provider reads the caller's
stored token, reports a rejected/expired credential, renews an expired one
with the stored refresh token (``renew_or_invalidate``), and records a 401 it
could not renew so the whole app agrees the sign-in is dead (see
``cloud.mark_cloud_token_invalid``).
"""

from __future__ import annotations

from backend.app.services.bambu_cloud_credentials import (
    get_stored_token,
    is_cloud_token_invalid,
    mark_cloud_token_invalid,
    renew_or_invalidate,
)

__all__ = ["get_stored_token", "is_cloud_token_invalid", "mark_cloud_token_invalid", "renew_or_invalidate"]
