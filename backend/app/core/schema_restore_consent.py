"""One-use, short-lived server receipts for the sole calibration exception.

Receipts are process-local (a restart requires a fresh confirmation), opaque,
bounded and bound to actor, ZIP, exact prepared DB/WAL, target and counts.
Only the replacement transaction turns a consumed receipt into durable audit.
"""

import hashlib
import secrets
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict
from datetime import datetime, timezone

from backend.app.core.schema_index_repair import CALIBRATION, manifest, quote

OBJECT = "filament_calibration.indexes." + CALIBRATION
CONFIRMATION_MESSAGE = (
    "This backup has duplicate active filament calibrations. Restore it with the same data and a warning, "
    "without the active-calibration uniqueness protection? No calibration rows will be removed."
)


def calibration_groups(path):
    row = next(r for r in manifest() if r["name"] == CALIBRATION)
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as source:
        columns = {r[1] for r in source.execute("PRAGMA table_info(filament_calibration)")}
        if not set(row["columns"] + ["is_active"]) <= columns:
            return 0
        names = ",".join(quote(c) for c in row["columns"])
        nonnull = " AND ".join(quote(c) + " IS NOT NULL" for c in row["columns"])
        return source.execute(
            "SELECT count(*) FROM (SELECT 1 FROM filament_calibration "
            f"WHERE is_active = TRUE AND {nonnull} GROUP BY {names} HAVING count(*)>1)"
        ).fetchone()[0]


def zip_digest(stream):
    digest = hashlib.sha256()
    stream.seek(0)
    for part in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(part)
    stream.seek(0)
    return digest.hexdigest()


def binding(authority, zip_hash, prepared, target, count):
    return {
        "actor": asdict(authority) if authority is not None else None,
        "zip_sha256": zip_hash,
        "source_sha256": [r[1] if r is not None else None for r in prepared.fingerprint],
        "target": list(target),
        "accepted_object": OBJECT,
        "duplicate_groups": count,
        "contract": "d4-v1",
    }


class ConsentReceipts:
    def __init__(self):
        self.entries = {}

    def issue(self, expected):
        now = time.monotonic()
        self.entries = {k: v for k, v in self.entries.items() if v[0] > now}
        if len(self.entries) >= 128:
            self.entries.pop(next(iter(self.entries)))
        token = secrets.token_urlsafe(32)
        self.entries[token] = (now + 300, expected)
        return token

    def consume(self, token, expected):
        entry = self.entries.pop(token, None) if isinstance(token, str) else None
        if entry is None or entry[0] <= time.monotonic() or entry[1] != expected:
            return None
        if expected["actor"] is None:
            return None
        return {**expected, "confirmed_at": datetime.now(timezone.utc).isoformat()}


receipts = ConsentReceipts()
