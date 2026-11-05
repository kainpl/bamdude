# BamDude local MCP connector

This is the source of the local stdio connector used by client packages. It is
not a second farm server. The official MCP SDK owns both protocols. There are
no REST, database, file-download or printer-command fallbacks.

The connector requires Node 22.19 or newer. A Desktop package must use the
client's bundled runtime; source tests alone do not establish Desktop support.
The client packages and guided installer are a separate implementation slice.

The host supplies configuration through its connection settings/environment:

| Setting | Meaning |
| --- | --- |
| `BAMDUDE_MCP_URL` | Exact `https://your-farm/mcp`; loopback HTTP remains allowed |
| `BAMDUDE_MCP_ALLOW_HTTP` | Only the literal `true` permits non-loopback HTTP. Off by default; trusted private LAN only, after both warnings in BamDude Settings. Keys and data are unencrypted. Does not disable HTTPS certificate verification. |
| `BAMDUDE_MCP_KEY` | Separate MCP-enabled key, supplied through the client's secret field |
| `BAMDUDE_MCP_CA_FILE` | Optional explicit PEM CA file for a self-signed LAN server |

Do not put a key in arguments, packages or chat. Never disable TLS verification.
All redirects are refused. A 401 remains a refusal, without OAuth discovery.
Changing the endpoint or trusted CA requires changing the client's configuration.
Two keys/configurations run in separate processes. No credentials are persisted.

Startup checks diagnostic/skill contract 1. Read-only tool discovery is refreshed
before direct tool calls; the farm revalidates the key on every request. The
connector limits incoming messages to 32 KiB, responses to 128 KiB, concurrent
forwarded operations to two, and HTTP calls to ten seconds. Cancellation and EOF
abort its owned HTTP requests. Stdout contains only SDK protocol messages;
stderr contains fixed error codes, never remote exception text or credentials.

For development, run `npm ci --ignore-scripts`, then `npm test` in this directory.
Tests use disposable loopback servers, generated certificates and synthetic keys.
They do not configure a personal AI client or contact a farm. Dependencies are
pinned in the lockfile; generated client packages belong outside the checkout.

The canonical skill remains in `../agent-skills/bamdude-diagnostics/`. The base
module offers capabilities and its guide; farm diagnostic tools arrive separately.
To disconnect: revoke the key in BamDude, remove the connector/skill in the client,
then optionally disable MCP in BamDude.
