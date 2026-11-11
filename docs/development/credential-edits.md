# Editing integration credentials

This contract is in development after stable 0.7.3. It covers notification
providers, application integrations, SMTP, smart plugs, Git backup, OIDC and
external-camera endpoints. Printer MQTT access codes keep their separate
existing printer-editor contract.

Read responses contain presence flags, such as `secrets_configured` for
notification providers or `has_token` for Git backup. They do not return the
credential, including to its editor or an administrator. A configured flag or
display label is not a credential and must never be submitted as one.

Requests accept an optional `secret_actions` map:

| Action | Credential field in the request | Result |
|---|---|---|
| `keep` | Must be omitted | Keep an existing stored value; unavailable on create or when none exists |
| `replace` | Must contain a nonempty value of the field's type | Replace the stored value |
| `clear` | Must be omitted | Remove it, subject to the adapter's disabled or explicitly anonymous mode |

An omitted action and omitted credential keep the value on update. A supplied
legacy nonempty value replaces it. Existing SMTP blank/null compatibility still
means keep; use the explicit clear action to remove an SMTP password. Other
adapters retain their own legacy empty/null validation. Literal `*` characters
in an explicitly replaced credential are real characters, not a mask protocol.
Unknown action fields, unknown actions and action/value conflicts are rejected
without outbound requests.

Each retained credential belongs to its effective destination: transport,
host/port, base path and adapter-specific account or recipient fields. Changing
that scope requires a replacement credential or an allowed clear. This applies
to both save and draft-test endpoints. It includes effective defaults and
environment precedence; an environment HA token cannot be rebound by changing
only its database URL and is never copied to storage or a response.

For example, keeping a Telegram provider's token while renaming it:

```http
PATCH /api/v1/notifications/12
Content-Type: application/json

{"name":"Farm alerts","secret_actions":{"bot_token":"keep"}}
```

Replacing it:

```json
{"config":{"bot_token":"new-credential"},"secret_actions":{"bot_token":"replace"}}
```

A supplied notification `config` still replaces its nonsecret configuration.
Include the nonsecret fields you want to retain. Stored secret values are
merged separately and can only be retained for the same provider type and
destination. A disabled provider can clear required credentials:

```json
{"enabled":false,"secret_actions":{"bot_token":"clear"}}
```

Credentials must stay in JSON bodies, never query strings. Git configuration
actions name `access_token`; Git draft-test actions name `token`, matching that
endpoint's field. A stored Git test uses only the stored configuration. The
legacy global virtual-printer writer also accepts a JSON body; consult its
OpenAPI request schema for field names.

Camera reads omit the complete stream and snapshot URLs, exposing presence and
a safe host separately. Save their full replacement values through the printer
editor. Test a saved snapshot with:

```http
POST /api/v1/printers/7/camera/external/test
Content-Type: application/json

{"source":"snapshot"}
```

Using a stored URL requires `printers:update` as well as `camera:view`. Testing a
new supplied URL keeps the existing camera-view permission requirement:

```json
{"url":"http://camera.lan/frame.jpeg","camera_type":"snapshot"}
```

Connection editors use an explicit Save connection action for their destination
and credential drafts. Testing a draft does not schedule a later save; ordinary
preference autosaves leave that connection pair unchanged.

Tests do not persist drafts. They release the configuration transaction before
calling the existing sender or camera worker. Partial Git restore cannot change
local credential destinations around a retained secret; edit those integrations
locally. Full private backups continue to preserve their credentials.


Standalone cameras use the same opaque-source contract. `/cameras/` reads omit
`url` and `snapshot_url`, returning `url_configured`, `snapshot_url_configured`
and `source_host`. Their editor keeps blank source inputs private, tests drafts
without persistence and saves only on explicit Save. Saved-source tests use
`POST /cameras/{id}/test` with optional draft fields, `secret_actions` and
`source: "stream" | "snapshot"`; they require `settings:update` and
`camera:view`. New, explicitly supplied sources can still use `/cameras/test`
with the existing camera-view permission. Clearing the primary source requires
a disabled camera; changing its type requires an explicit replacement source.
`PATCH /api/v1/cameras/{id}` rejects unknown or read-only fields with 422; send
only editable fields and `secret_actions`, not an object copied from GET.

MQTT connection fields and Prometheus exposure/token edits also require explicit
Save connection. Ordinary preference autosave never carries those drafts.
Enabling metrics now requires a nonblank token; clear it only while disabled.
SMTP authentication can be turned off while retaining the unused password for
the same host/account, and no login/password is sent in that mode. LDAP tests
preserve the operator's stored CA verification policy; partial Git restore cannot
replace or remove that CA path around a retained bind password.

Legacy smart-plug fields accepted by older PATCH writers remain editable when
unchanged, while actual changed values obey create-time field constraints.
HTTP client wire INFO/DEBUG logging stays disabled even in application debug
mode because complete URLs and headers may carry opaque credentials; adapter
logs retain safe endpoint hosts and typed failure categories.
