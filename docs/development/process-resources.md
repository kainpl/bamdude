# Process limits, SQLite pooling and support diagnostics

SQLite defaults to 10 persistent connections plus 90 overflow connections per
engine. In WAL mode SQLite retains main-database descriptors up to the peak
number of simultaneous connections while another connection still holds a lock;
later bursts reuse them. This is bounded retention, not a growing leak.

The peak demand of a real farm has not been measured. Above the default capacity
of 100, requests wait for the pool timeout (normally 30 seconds), then fail with
`TimeoutError`. Operators can override `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` and
`DB_POOL_TIMEOUT`. PostgreSQL defaults remain 20 + 60 per engine.

On systems with `RLIMIT_NOFILE`, startup raises the soft descriptor limit after
the startup heap policy and before children start. The target is 65536, capped by
a finite hard limit. An already higher or unlimited soft limit stays unchanged;
the hard limit never changes. A permitted 10240 fallback is best effort. Windows
and restricted environments keep working if the operation is unavailable or
refused. Children inherit the resulting limits.

Paho still uses `select`, which cannot handle descriptor numbers of 1024 or more
on affected POSIX systems. A high connection socket can recover on its own; a
high wake-up socketpair requires a new client. The printer watchdog now also
considers disconnected clients that never received a message, using the age of
their connect attempt. It retains the five-minute grace, ten-minute rebuild
cooldown and reachable-MQTT-port requirement. A reachable printer with invalid
credentials can therefore also be rebuilt periodically. During the grace,
paho may make periodic TLS attempts (about 14 in 300 seconds in the measured
case); this alone does not indicate a printer fault.

If `connect()` itself raises before the manager registers a client, that old
failure path is outside this watchdog change. It remains possible at descriptor
exhaustion, including exhaustion of a successfully raised limit. MQTT relay and
smart-plug clients do not use the printer watchdog. Their failure to retry after
an initial descriptor-exhaustion error already existed at a soft limit of 1024;
raising the limit changes that symptom to a sticky high socketpair, not its
recovery policy. Reconfiguration or restart may be needed for those services.

Both support ZIPs and issue packs include `process.file_descriptors`:

- `count`: best-effort current-process FD count; children are excluded.
- `kinds`: bounded Linux readlink-text classification, never target filesystem
  `stat` calls. Directories and other ordinary paths share `other_regular`.
  Deleted `.db`, `-wal` and `-shm` files retain their categories. These names do
  not establish ownership by BamDude.
- `soft_limit` and `hard_limit`: `-1` means the platform's `RLIM_INFINITY`;
  `null` means unavailable. At exhaustion limits may still be available when
  count and kinds are not.
- `kinds_truncated`: classification stopped after 4096 entries. Total count is
  not truncated.

The count and classification are separate, non-atomic snapshots. Linux counting
usually includes one temporary listing descriptor; classification often sees
that closed entry as one `disappeared`. Concurrent reuse can change this. Neither
observation establishes a leak. No paths, targets, descriptor numbers, child
arguments or credentials are exported. Collection precedes the high-RSS census
skip and also survives failure to inspect the main process.
