# Credential lifetimes

This independently accepted contract is implemented in `feature/v0.7.3-fixes`.
It is not included in the stable 0.7.3 release.

Accounts have an immutable identity, independent of username and numeric row
id. A browser session has its own server-side record and records the actual
authentication source (`local`, `ldap` or `oidc`). Access JWTs carry the account
identity, session generation and session id. A signed token containing only
`sub` is insufficient. Refresh rotation stays within the same session family.
Signing out revokes that family; ending sessions advances the account's
generation. Password and MFA recovery changes invalidate dependent session
credentials in the same short transaction as the change.

API keys use their existing immutable connector identity. New keys and
long-lived camera/monitor tokens bind to an immutable owner, rather than
inheriting whichever account later receives the same numeric id. They have
independent lifetimes: browser logout and session invalidation do not revoke
them. Their current enabled/revoked state, owner and permissions still apply.
One-time login, setup, exchange and reset credentials, and reusable media,
WebSocket and camera derivatives, carry their issuing authority's binding.
Validation uses the current database; storage outages return retryable 503
responses, without deleting otherwise valid browser or kiosk credentials.

## Updating an existing installation

The upgrade deliberately retires **all legacy API keys and long-lived tokens**,
including ownerless keys, disabled keys and expired or previously revoked
tokens. It does not infer ownership from timestamps or username matches.
Legacy browser sessions and unbound login/setup/reset attempts are rejected;
sign in again. Current-contract credentials survive an ordinary restart.

An administrator sees retirement counts and pending/delivered notice state in
Settings and the support projection. Retired rows remain identifiable. Create
replacement keys for a specific active owner and choose their scopes and
printers explicitly; a replacement form does not copy old broad permissions.
Generate new camera and monitor tokens through their existing Settings flow.
Raw credentials are shown only when issued and cannot be recovered from the
retired row.

Owners and administrators can remove a retired camera/monitor token record
after confirmation, using the existing scope permissions. This only removes
its list entry: the old secret stays invalid, and historical retirement counts
remain available in Settings and support.

Token lists include their non-secret `token_identity`. Settings sends it as
`expected_identity` for both revoke and metadata removal, adding
`retired_only=true` for removal. A stale selection cannot affect a replacement
at the same numeric id, and metadata removal cannot fall through to revocation
of a current token. Existing numeric-only API revoke calls retain their old
contract; clients acting on saved list entries should supply the identity.

Check each consumer you operate after reissue:

| Consumer | Replacement to configure |
| --- | --- |
| Home Assistant, REST scripts and webhook senders | New owner's API key and the necessary scopes |
| BamDude Bridge or another API client | Its configured API-key header |
| MCP adapter | `BAMDUDE_MCP_KEY`, then reconnect the adapter |
| OrcaBamDude slicer connector | New API key; old key namespaces and receipts are not inherited |
| API-key camera viewers | New scoped key; keep the existing current-access rechecks |
| Camera/monitor tokens, camera wall and overlay links | Fresh token for the required `camera_stream`, `camwall`, `overlay` or `monitor` scope |
| Browser tabs, TV links and media/WebSocket URLs | Sign in again and obtain fresh derivative links |

Do not place a replacement secret in a query log, support report or editable
notification template. Backend integrations that are not API-key consumers
continue using their own credential-editing contract.

## Final writers and restore

Delayed operations capture scalar authority before hashing, network calls,
downloads or file preparation. Final publication takes the immutable account
guard before database domain rows, then checks the credential, current grants,
printer scope and source visibility on the writer's own session. Preparation
does not authorize a later write. Inner callbacks use the guard already held;
they do not resolve a raw key or open another audit transaction. Existing
storage and queue process guards keep their established outer ordering.

Cloud snapshots additionally bind provider credential generation, immutable
owner, connector storage epoch and runtime epoch. Replacing credentials clears
that owner's obsolete provider bindings; refresh cannot overwrite a newer
credential. A lost owner never becomes the global cloud account. Explicit
trusted server/global modes remain separate from authenticated requests.

Restore closes admission and drains owned authentication, slicing, scan and
rollout work before replacing storage. If drainage cannot be proved, restore
refuses replacement. Authentication invalidation and connector epoch rotation
commit together, before the durable restore fence is removed. A crash, failed
commit or failed fence removal keeps admission fenced until startup completes
the same recovery. Restore does not reopen the running process; restart is
required. An archive remains one physical print's history, even when files are
deduplicated or the original account no longer exists.

## Informational notices

Migration records retirement facts only. After ASGI startup completion has
opened technical authentication admission, an owned runtime delivers the
administrator inbox notice. An operational farm pause does not prevent this.
Template readiness waits at most five seconds within a ten-second attempt;
pending attempts retry after sixty seconds. Render, commit, timeout and live
socket failures cannot hold authentication admission closed.

The internal inbox path stages rows and the episode marker in one caller-owned
transaction, then dispatches scalar live hints after commit. Unique episode and
recipient keys prevent repeat durable delivery. Hints address the immutable
account as well as its numeric id and open no database session. Existing
ordinary `deliver()` callers retain their original commit behavior.

This unit establishes credential identity and final-write checks. Continuous
revalidation of all already-open sessions/streams belongs to G01 D; existing
connector camera revalidation remains in force.
