"""Opaque external-camera endpoints; transport remains owned by the worker."""

from fastapi import HTTPException

from backend.app.core.credential_config import merge_secret_fields

CAMERA_SECRET_FIELDS = frozenset({"external_camera_url", "external_camera_snapshot_url"})


def resolve_camera_credentials(stored, changes, actions=None, *, creating=False):
    resolved = merge_secret_fields(
        stored,
        changes,
        CAMERA_SECRET_FIELDS,
        actions,
        creating=creating,
        clear_values=dict.fromkeys(CAMERA_SECRET_FIELDS),
    )
    if (
        stored.get("external_camera_url")
        and not resolved.get("external_camera_url")
        and resolved.get("external_camera_enabled")
    ):
        raise HTTPException(400, "Disable the external camera before clearing its stream URL")
    return resolved
