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


STANDALONE_SECRET_FIELDS = frozenset({"url", "snapshot_url"})


def resolve_standalone_camera_credentials(stored, changes, actions=None, *, creating=False):
    """Standalone rows own their sources; never borrow a printer's identity."""
    if any(key in changes and changes[key] is None for key in ("name", "url", "camera_type", "rotation", "enabled")):
        raise HTTPException(422, "Required camera fields cannot be null")
    resolved = merge_secret_fields(
        stored,
        changes,
        STANDALONE_SECRET_FIELDS,
        actions,
        creating=creating,
        clear_values={"url": "", "snapshot_url": None},
        scope={"url": lambda config: config.get("camera_type")},
    )
    if resolved.get("enabled", True) and not resolved.get("url"):
        raise HTTPException(400, "Disable the camera before clearing its source URL")
    return resolved
