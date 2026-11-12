# Virtual-printer retirement and restore

Restore requires an idle farm, with no physical prints or transfers. The VP
owner drains local work; it does not stop physical-printer completion writers.
Transparent proxy connections are deliberately disconnected, including remote
FTP/FileTransfer. Their counts describe interruptions. Raw camera leases are
reported separately from connections.

An upload accepted before closing finishes its normal library/capture/queue
workflow. A later slicer command can be refused. Git does not restore the queue,
so inspect pending jobs and their options before restarting/resuming; resending
can create a second job. A409 asks to wait and retry in the same process, then
restart after restore or to abandon it. Retry never reopens VP admission.

The lifetime owner, sessions and transport proofs are in
`services/virtual_printer/lifecycle.py`. The manager's lock publishes retirement
and closes listeners only; it never surrounds a network or database wait.
Ordinary edit keeps its saved configuration, with a stopping/unproven status
if sync/retirement is still pending. Delete preserves files used by an owned
callback. Replacement waits for predecessor proof and reads the latest edit.

Regression modules:

```powershell
python -m pytest backend/tests/unit/services/test_vp_owned_drain.py backend/tests/unit/services/test_vp_startup.py backend/tests/integration/test_vp_drain_publication.py backend/tests/integration/test_vp_drain_restore_boundary.py backend/tests/integration/test_vp_drain_crud.py -n4 -q --timeout=120 --timeout-method=thread
```

The socket cases bind only127.0.0.1:0, with private temporary certificates.
They cover ordinary stop/remove/shutdown/restore, idle FTP, MQTT CONNECT and
pre-CONNECT, delayed real bind, a TLS peer which never answers close_notify,
and persistent TCP/TLS proxy streams. Run the socket subset on Linux as well
as Windows. The publication test uses real library, capture and queue-source
transactions, with a barrier after the library commit; only queue selection
is fixed by its harness. No file is sent to hardware.

Fault controls should detect missing admission/latch, prematurely quiet business,
lost timer/client ownership, and a full-budget TLS graceful close. The exact
selection, environment and results belong in each external acceptance packet.
