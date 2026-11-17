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

Full ZIP restore closes auth, cloud, slicing and library-scan admission before
its first await. It publishes the restore hold, ends its read transaction and
proves VP completion before joining those owners. Library upload-write and
connector drains remain later barriers; the original restore actor is checked
again under the storage guard, and the connector fence is durable before any
file replacement. Git metadata restore keeps its separate category-only path.

The merged boundary is covered by `test_g02_f22_restore_order.py` and
`test_g01_c_restore_lifecycle.py`. These include a refused drain with an accepted
VP callback still running, an actual blocked upload thread, revoked restore
authority and the on-disk connector fence at the file-apply boundary.

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
