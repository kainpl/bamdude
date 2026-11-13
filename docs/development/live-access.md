# Live access and revocation

This is the implementation contract for G01 D. The candidate requires independent
acceptance before integration into the fixes line. Credential identity and the
upgrade/reissue procedure remain in [credential lifetimes](credential-lifetimes.md).

## Selecting a credential

An explicitly present `X-API-Key` selects API-key authentication, even when empty.
A Bearer `bd_` or `bb_` credential also selects this path. An invalid selected key
returns 401; it cannot fall back to a valid browser JWT or query token. A valid key
still needs the route's permissions and printer scope. Browser-only and token-only
doors do not gain API-key support. Remove an obsolete API-key header when using a
browser, camera-wall, overlay, monitor or slicer URL token. Ordinary JWT/query-token
precedence is retained when no key is selected. Authentication overload/storage
uncertainty returns retryable 503, rather than clearing otherwise valid credentials.

The `/ws` upgrade remains query-token only. It also rejects a selected key header
(including an empty `X-API-Key` or key Bearer) before verifying the query token;
WebSocket upgrades do not pass through the HTTP authentication middleware.

## Already-open channels

WebSockets and covered camera/private-media responses capture their exact issuing
account and credential. Admission performs a fresh durable read after acceptance
and before private delivery. Current enabled state, owner, permissions, printer
scope, addressed resource incarnation and applicable connector namespace remain
authoritative. Logout ends that browser session, without ending another device's
session or independently issued API/long-lived credentials.

A single coordinator batches reads by credential family. Its one-second refresh,
three-second proof lease and one-second read deadline bound revalidation. There
are at most 1,024 live handles, one actual batch task and 256 projection classes;
SQL identity lists use chunks of 256. These are process limits, not a capacity
promise for a particular host. Admission overload is retryable. A timed-out or
cancelled waiter never releases the actual database work or its slot prematurely.

A revocation committed before the admission read starts permits no private send.
A later commit stops new private sends within five seconds; there is no pre-commit
mutation fence. A send already entered at the server boundary may finish and
bytes already passed to the network cannot be recalled. Presented token expiry
also ends its channel, even when its parent remains valid; the client can obtain
a fresh derivative using that parent. A raw API-key camera admission additionally
retains its original absolute 300-second lease.

WebSocket output uses current permission/printer/ownership classes for bootstrap,
direct, targeted and broadcast delivery. Unknown event types are omitted. Private
print names require a proven current execution and archive access; operational
status can remain visible without those names. Personal inbox payloads remain
browser-session only. Each socket has one bounded writer; a slow viewer's cleanup
does not stop another viewer or a shared camera producer.

| WebSocket close | Meaning | Client behavior |
| --- | --- | --- |
| 4401 | Issuing credential/parent is invalid | End authentication flow |
| 4403 | Current `websocket:connect` permission is lost | Stop reconnecting |
| 1001 | Presented token expired, restore or shutdown | Reconnect/remint when permitted |
| 1013 | Uncertain proof, capacity or changed projection class | Back off, preserve credentials, bootstrap again |

## Media delivery and cleanup

The covered inventory is the route registry in `services/live_media.py`: printer
and standalone camera streams/snapshots, camera covers, private library/archive/
Workshop images and timelapse video. Route coverage is tested against the actual
application by HTTP method and path. Only GET/HEAD media responses use this
lifecycle; POST/PUT/DELETE handlers sharing those URLs retain their ordinary
transaction/response lifecycle. A successful covered response without its bound
viewer fails closed.

Archive thumbnails retain the trash UI's existing visibility policy. That route
alone opts into trashed archive rows after its usual visibility check; current
credential, ownership, printer scope and captured incarnation still apply. Other
archive media and deleted library resources do not inherit this exception.

Body production and the outer pure-ASGI delivery gate both check the viewer. The
outer gate is outside the buffering HTTP middleware, so buffered chunks cannot
start a new server-facing send after retirement. Video is checked per 64 KiB body,
including single and multipart Range responses. HEAD, ETag, If-Range and 416
semantics are preserved. File I/O is off the event loop and remains owned until
the actual thread finishes.

Retirement cancels only the viewer's body/writer. A separate owned cleanup joins
that work, closes iterators/files and releases subscription/light references;
ordinary disconnect uses the same path. Restore waits for actual checks and
cleanup, refusing replacement if drainage cannot be proved. A deliberately
aborted response can produce uvicorn's `ASGI callable returned without completing
response.` ERROR. The accompanying fixed INFO line `Live media delivery stopped:
category=authority_retired` identifies this expected access termination without
logging a token or URL.

## Color catalog sync and exclusions

Color sync retains its existing SSE writer and public provider. Each page rechecks
the original actor on the transaction that writes it; counts advance only after
commit. A refused or failed page emits an error, never `complete`. Restore owns
the actual page transaction through close. Count-only progress has no separate
private-delivery watcher.

Generic finite exports/downloads, one-use slicer file delivery and ordinary polling
do not gain a continuous body watcher. Their request authentication still follows
the explicit-key rule. No archive identity, physical-print history or queue model
is changed by this contract.
