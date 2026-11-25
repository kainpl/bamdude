# Invariants: Authentication, permissions and tokens

Full text of the rules that [`CLAUDE.md`](../../../CLAUDE.md) keeps in short form, with the reasons and the history behind them. `Vault:` pointers name notes in the maintainer's private knowledge base; everything a contributor needs is here.

## Auth always-on

Every endpoint via `RequirePermission(Permission.X)`. Setup-gate middleware returns `503 setup_required` until `POST /auth/setup` creates admin (whitelist: `/auth/status`, `/auth/setup`, `/system/health`). API keys (`X-API-Key` / `Bearer bd_...`) bypass JWT but satisfy perms. New keys are `bd_…`. The G01 C upgrade retires every legacy key, including Bambuddy's `bb_…` keys; replace them with newly issued owner-bound keys. Prefix recognition alone never grants current-contract validity. The prefix only tells a Bearer key from a JWT, and `core/auth.is_api_key_token` is the one place that asks — `tests/unit/test_api_key_prefix_is_asked_in_one_place.py` fails on a hand-written `startswith("bb_")`. Recovery: `python -m backend.app.cli reset_admin`.

## Credential identity and sliding refresh

`core/auth_lifetime.py` owns immutable account UUIDs, session generations and server-side sessions. JWTs name account + generation + session; username-only JWTs are refused. The session records the actual local/LDAP/OIDC source. Refresh hashes rotate inside the same session family; logout revokes that family, while account session invalidation advances its generation. API keys retain their ONE existing `connector_identity`; keys and long-lived tokens bind to an immutable owner and have independent lifetimes, so ending browser sessions does not revoke them. All legacy keys/tokens require reissue; never guess ownership from a timestamp. Delayed writers capture scalar authority before slow preparation, acquire the lifetime guard before domain DB rows, and recheck on the publication session; no inner raw-key resolver/audit session. Restore drains owned work and commits auth invalidation + connector epoch together before clearing its durable fence. Informational rollout fan-out starts only after technical ASGI admission, never gates sign-in. Vault: `40-invariants/inv-auth-credential-belongs-to-immutable-account.md`. See [credential lifetimes](docs/development/credential-lifetimes.md).

## 2FA + OIDC

TOTP + email OTP + 10 backup codes. Login returns `requires_2fa=True` + `pre_auth_token` when enabled. OIDC: PKCE S256 + state + nonce + JWKS; SSRF guards reject non-HTTPS/loopback/private/link-local. TOTP secrets + OIDC `client_secret` Fernet-encrypted; `MFA_ENCRYPTION_KEY` auto-bootstraps (env → `DATA_DIR/.mfa_encryption_key` → generated, mode 0600). Backup ZIP packages key alongside DB.

## Rate limiting

Sliding-window in `auth_rate_limit_events`. Login 10/15min per user + 20/15min per IP. Behind proxy: set `TRUSTED_PROXY_IPS`.

## Permissions

`resource:action` (`printers:control` etc.) in `core/permissions.py`. Default groups: Administrators / Operators / Viewers. **API-key scopes** confine a key's reach: `core/auth.py` maps every `Permission` either to a `can_*` column in `_APIKEY_SCOPE_BY_PERMISSION` (`can_manage_library`/`inventory`/`projects`/`archives`/`maintenance`, `can_queue`, `can_read_status`, …) or onto `_APIKEY_DENIED_PERMISSIONS` (admin-only — e.g. `ARCHIVES_PURGE`/`LIBRARY_PURGE`/`PIPELINES_WRITE`). **Every new `Permission` MUST land in exactly one** (a drift-guard test enforces it) **and be seeded to Administrators in its migration** (O2 discipline — Administrators aren't self-healed at startup, our migrations are frozen).

## An API key's printer list is enforced in the gate, not in the routes (`core/api_key_scope.py`)

`APIKey.printer_ids` — `None` every printer, `[]` none. The three gate factories (`require_permission` / `require_any_permission` / `require_ownership_permission`) take the `Request` and call `enforce_printer_scope` right after `authorize_api_key`, so no route can forget it; a caller that invokes a checker directly (`routes/monitor.py`) must pass `request=`. It reads printers BY NAME: `printer_id` / `printer_ids` / `queue_id` in the path, the query and **anywhere in the JSON body** (FastAPI parses and caches the body before it solves dependencies), plus `mqtt_printer_ids` (comma list, parsed exactly as the support route parses it); jobs via `source_queue_item_id` / `queue_item_ids` anywhere and, on `/queue/…` only, `{item_id}`, `{batch_id}`, `item_ids`, `cancelled_item_ids` (bulk-retry — read by the gate itself, not left to the body validator that runs after it), `items[].id`; and `_PATH_RESOLVERS` for things that belong to one printer (plug, maintenance item, dispatch job, calibration session). A value it cannot read as an id is REFUSED — pydantic coerces `"2"` and `2.0` to 2, so an unreadable reference must not pass; a name that resolves to nothing passes and the route 404s. `/auto-queue` is closed to a restricted key as a whole (the distributor picks the printer). Lists narrow themselves via `key_printer_scope(request)` / `in_key_scope` — printers, status batch, queues, queue items, monitor snapshot; history and statistics are NOT printer-scoped. ⚠️ A new printer reference under another name (`target_printer_id`) slips past the reader: `tests/unit/test_api_key_printer_scope_coverage.py` walks every key-reachable route and fails until the name is read or listed in `NOT_A_PRINTER` with a reason. Vault: `40-invariants/inv-api-key-printer-scope-in-the-gate.md`.

## Camera stream-token — the camera only

A printer's or standalone camera's stream/snapshot and the plate-detection reference picture need the 60-min token from `POST /printers/camera/stream-token` (`camera:view`), or a long-lived `STREAM_SCOPES` token for kiosks. `<img src>` can't send Auth headers → `withStreamToken()` threads token via query param. `useStreamTokenSync` keys cache on `user.id`; `rewriteMediaSrcWithToken(root, token, applies)` retrofits already-rendered media, each token only onto the paths that take it.

## Every other picture takes a MEDIA token and checks its own resource (audit D9 a2, 2026-09-26)

Archive/library thumbnails and plate thumbnails, an archive's photos, `plate-preview`, `project-image`, `qrcode`, the timelapse video, `card-file`, product and order covers, the printer card's `camera-cover` (the job's picture, not the camera), MakerWorld covers and the CDN proxy take a media token from `POST /auth/media-token` (any signed-in user, JWT only — an API key sends its header) in `?token=`, or the ordinary headers. The token records the user; the route dependency — `core/auth.require_media_permission` / `require_media_ownership_permission` — applies the resource's own rule (`_ensure_archive_visible` / `_ensure_library_file_visible`, `include_trashed` only for the trash lists' thumbnails; `orders:read` / `products:read` for order and product pictures, `printers:read`, `makerworld:view`), and the header path runs the ordinary checkers, so API-key scopes and a key's printer list hold. A password change or deactivation ends the token. The two tokens are NOT interchangeable, and no long-lived Cam Wall / overlay / kiosk token opens a picture. The one exception (owner, 2026-11-15): `GET /overlay-branding/logo` serves the installation's stream-overlay logo under the `overlay` token alone (`RequireOverlayToken`, its own anchored pattern) — a branding image with no owner or printer to check, not a user's resource, and OBS opens the overlay without a session; it serves that one file and nothing else. Anonymous by the owner's decision: a notification's short-lived photo copy (`GET /notifications/photos/{filename}`) and the external-link and OIDC icons. ⚠️ A notification never links the archive's own photo (owner, 2026-11-17 — the anonymous archive route kept causing trouble): `{finish_photo_url}`, and the picture Home Assistant / Bark / a Slack-format webhook fetch themselves, lead to a COPY written by `utils/notification_photos` through `NotificationService.photo_link` — the 192-bit file name is the credential, the route serves only names of that shape, and a copy older than three days is refused even before a sweep removed it; the copies are derived (not in backups, not in the database); `PUBLIC_ROUTES` tags every entry `media-token` or `anonymous` with its reason. Frontend: `withMediaToken()` stamps only `/api/v1/…` paths, so a server-supplied URL can be wrapped blind; `isCameraMediaPath()` alone decides which token a path takes; `useStreamTokenSync` fetches the media token for every user and the camera token only with `camera:view`. ⚠️ A cache-buster (`?v=`, `?view=`) goes INTO the builder, before the token — appended after, the second `?` lands inside the token's value. Vault: `40-invariants/inv-media-token-checks-the-resource.md`.

## A route called without an Authorization header reaches its own token check only through its OWN anchored pattern

`main.PUBLIC_API_PATTERNS` holds one `^…$` regex per such route, and `tests/test_auth_public_patterns.py::PUBLIC_ROUTES` names each with the gate it carries — a neighbouring pattern never covers a new segment (`/archives/{id}/source-dl/…` had none and was 401 on every install until 2026-09-24, upstream #3029). The three slicer downloads (`/dl/`, `/source-dl/`, library `/dl/`) verify with `verify_slicer_download_token(..., single_use=False)`: a protocol handler hands the URL to a process we do not control, which retries, resumes and gets scanned, so the token stays valid for its TTL and bound to its resource; the one-shot default is for a download that consumes itself. Vault: `40-invariants/inv-token-in-url-routes.md`.

## A retained integration credential belongs to its effective destination

Public reads expose presence, never the value, and save and draft tests share keep/clear/replace resolution. Every pair writer takes the pre-existing installation credential anchor before authoritative adapter rows (existing outer resource guards stay outermost); PostgreSQL locks it and SQLite reserves its writer before reads. Missing anchor fails closed; bootstrap/full restore creates it before admission. Freeze the validated pair and finish the short transaction before network I/O. Partial restore cannot rebind local credential destinations; full private backups retain credentials. Vault: `40-invariants/inv-integration-credential-belongs-to-destination.md`.

## Live delivery needs current authority and owned cleanup

The live coordinator
batches exact credential/account/target reads; admission rereads after accept,
and short memory proofs guard every send. Mutation hints do not replace durable
reads or create a pre-commit fence. The outer pure-ASGI media gate sits outside
buffering HTTP middleware and fails closed for covered routes without a viewer.
A separate cleanup task owns body/writer join and resource release through
actual completion, also on ordinary disconnect; restore refuses an unproven
drain. One viewer cannot stop the shared camera producer. Private WS output
uses current projection classes on every client path; trusted internal taps
keep their server contract. Already-entered network sends cannot be recalled.
Vault: `40-invariants/inv-live-delivery-requires-current-authority.md`.
