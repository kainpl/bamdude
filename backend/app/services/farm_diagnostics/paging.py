"""Process-bound, signed keyset cursors; never a substitute for authorization."""

import base64
import hashlib
import hmac
import json
import secrets
import time

from backend.app.mcp_server.contracts import DiagnosticAccess
from backend.app.services.farm_diagnostics.access import DiagnosticError

_SECRET = secrets.token_bytes(32)


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _actor(access: DiagnosticAccess) -> str:
    return _digest(
        [
            access.key_id,
            access.owner_id,
            sorted(access.permissions),
            access.is_admin,
            None if access.printer_ids is None else sorted(access.printer_ids),
            access.logs_allowed,
        ]
    )


def encode(access: DiagnosticAccess, tool: str, filters: dict, *, last: int, upper: int) -> str:
    payload = json.dumps(
        {
            "v": 1,
            "tool": tool,
            "filters": _digest(filters),
            "actor": _actor(access),
            "epoch": access.epoch,
            "last": last,
            "upper": upper,
            "expires": int(time.time()) + 600,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    signature = hmac.digest(_SECRET, payload, "sha256")
    return base64.urlsafe_b64encode(payload + signature).decode().rstrip("=")


def decode(token: str, access: DiagnosticAccess, tool: str, filters: dict) -> tuple[int, int]:
    try:
        if len(token) > 2048:
            raise ValueError
        raw = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        payload, signature = raw[:-32], raw[-32:]
        if not hmac.compare_digest(signature, hmac.digest(_SECRET, payload, "sha256")):
            raise ValueError
        data = json.loads(payload)
        if (
            data["v"] != 1
            or data["tool"] != tool
            or data["actor"] != _actor(access)
            or data["filters"] != _digest(filters)
            or data["epoch"] != access.epoch
            or data["expires"] <= time.time()
        ):
            raise ValueError
        if type(data["last"]) is not int or type(data["upper"]) is not int or not 0 < data["last"] <= data["upper"]:
            raise ValueError
        return data["last"], data["upper"]
    except (ValueError, TypeError, KeyError):
        raise DiagnosticError("invalid_cursor") from None
