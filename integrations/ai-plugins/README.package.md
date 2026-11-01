# BamDude AI

The base server offers capabilities and its usage guide. Farm diagnostics are
not available until the server explicitly advertises that module. Package
installation alone does not prove a working MCP connection or grant permission.

Create a separate MCP diagnostic key in BamDude Settings, choose its printers,
and keep log access off unless needed. Your own permissions still apply. Enter
the key in the client's protected configuration, never in chat or a package.
Data returned to the assistant may be sent to your chosen model provider.

Claude Desktop: open the MCPB through Desktop Extensions and fill in its user
configuration. It uses Desktop's bundled Node (22.19 or newer is required).
MCPB does not install the skill: download the portable skill ZIP in BamDude and
install it separately using your client's Skills UI. Desktop install/runtime
compatibility is unverified until documented in the support matrix.

Claude Code: extract its plugin ZIP and use its plugin installation UI or
session-only `--plugin-dir` option. Supply address/key/optional CA in the plugin's
user configuration. This connector requires Node 22.19 or newer on that computer.
The package includes the canonical skill under `skills/`.

Portable/OpenAI plugin: install the plugin through your client's local plugin
flow. It supplies the skill. Portable plugins have no portable secret-entry
mechanism, so configure the MCP connection separately using the example below
and your client's credential environment. Do not substitute a literal key into
the downloaded package. Public-store or universal-client support is not implied.

```toml
[mcp_servers.bamdude]
url = "https://YOUR-FARM/mcp"
bearer_token_env_var = "BAMDUDE_MCP_KEY"
```

Use HTTPS outside loopback. For a self-signed certificate, explicitly select its
PEM CA file in the local connector. When using an IP, the certificate must list
that IP. Never disable TLS verification. Redirects and OAuth discovery are
refused. If the farm was offline when the connector started, restart the client
or its MCP connection after the farm returns; there is no background retry.

Ask the assistant to read BamDude capabilities, then its guide. Check the
observed MCP request for this key in Settings; REST usage is not that proof.
The marker resets when the server restarts and does not prove skill installation.

To disconnect: revoke the key in BamDude, remove the plugin/connector in the
client, then optionally disable MCP. Uninstalling a plugin never revokes a key.

Full instructions and the current support matrix:
https://docs.bamdude.top/addons/ai-assistants/
