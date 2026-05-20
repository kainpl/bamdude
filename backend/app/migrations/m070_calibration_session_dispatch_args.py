"""Add ``dispatch_args_json`` to ``calibration_session``.

Flow Rate is a two-pass calibration: pass 1 (coarse, 9 blocks) prints,
the operator picks the smoothest block in the ``coarseSave`` step, and
pass 2 (fine, 10 blocks) prints centered on that coarse-pick ratio.

The pass-2 dispatch needs the *same* preset selection, bundle, bed
type, slicer, print options and swap macros the operator picked at the
start of pass 1 — but the wizard's ``start_calibration`` API doesn't
get called again for stage 2 (the wizard simply transitions step state
on the existing parent session). Without storing the original args
somewhere the backend can read, ``_start_flow_rate_stage2`` has no way
to invoke the slice + FTP + enqueue pipeline on its own.

``dispatch_args_json`` is the JSON-serialised snapshot of those args.
``start_calibration`` writes it onto the session row at session creation;
``_start_flow_rate_stage2`` reads it from the parent session, applies a
pass-2 + baseline-flow-ratio override, and re-runs the dispatch pipeline
for the new stage-2 session.

Idempotent — ``add_column`` is a no-op when the column already exists.
"""

from backend.app.migrations.helpers import add_column

version = 70
name = "calibration_session_dispatch_args"


async def upgrade(conn):
    await add_column(conn, "calibration_session", "dispatch_args_json TEXT")
