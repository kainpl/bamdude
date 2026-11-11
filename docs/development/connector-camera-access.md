# Connector camera access

Printer stream and snapshot routes accept API keys through `X-API-Key` or
`Authorization: Bearer`. Header authentication goes through the normal
camera-view permission gate; a bad header never falls back to a query token.
The key's status scope, active owner's camera permission and printer list apply.
The optional printer-identity header must match the printer's connector UUID.

API-key streams retain their 300-second absolute lease, but reconnecting at the
end of that lease is not their revocation mechanism. The interim OQ2 guard
checks current access before the first private frame and at most every two
seconds of delivery. Its database wait budget is one second, and an idle
subscriber checks at least once per second, within the five-second revoke bound.
The same check applies before releasing queued data after a viewer resumes.
It stops issuing new private chunks on revoke, delete, expiry, scope/owner
changes, printer replacement, storage-epoch change or an uncertain database.
Data already handed to HTTP or network buffers cannot be retracted.

The guard keeps scalar key incarnation/hash/owner and printer incarnation,
never the raw credential, request or an open session. It uses the connector's
existing UUIDs and read-only server/storage namespace; missing or malformed
incarnations fail closed. Each short read reloads current authority and uses
the shared API-key owner/permission gate. It neither hashes the key nor writes
`last_used` again. Expensive database work is time-based, not per frame.

Both initial stream configuration reads and periodic reads belong to the
existing connector runtime's owned-work drain. A cancelled/timed-out requester
cannot make a still-running query invisible to restore. The viewer closes on
uncertainty, and actual SQL/cleanup remains owned until completion. A closed
guard never reopens; the client reconnects through ordinary authentication.
No new status code can be sent after a streaming response's headers; EOF is
not a claim that a temporarily unverifiable credential is invalid.

Only the affected subscriber is removed. The existing worker and broadcaster
continue serving other viewers; no new physical camera reader is introduced.
JWT and query-token streams retain their existing policy. G01 D will replace
this per-viewer interim check with its shared live-authority coordinator, and
G01 C must include this consumer in its shared-validator adaptation inventory.

## Upload integration

Connector uploads revalidate the existing key before entering the shared library
writer, passing explicit API-key authority and the current owner's identity.
They do not enter the trusted server-ingest path.

The shared writer performs its initial file write off the event loop. Its
lifespan owner tracks the actual thread until it ends, including repeated
request cancellation. Restore closes write admission and drains those writes
before replacing storage; an unproven drain refuses restore. Only lifespan
startup can reopen this owner. This covers the initial raw write, not a new
transaction or lifecycle contract for the entire library ingest pipeline.
