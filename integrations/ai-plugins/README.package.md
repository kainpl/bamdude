# BamDude AI

The base server offers capabilities and its usage guide. Farm diagnostics are
not available until the server explicitly advertises that module. Package
installation alone does not prove a working MCP connection or grant permission.

Create a separate MCP diagnostic key in BamDude Settings, choose its printers,
and keep log access off unless needed. Your own permissions still apply. Enter
the key in the client's protected configuration, never in chat or a package.
Data returned to the assistant may be sent to your chosen model provider.

Claude Desktop: in Settings → Extensions → Advanced settings → Install extension,
select the MCPB. Enter the address and key, save and enable the extension. Leave
the optional CA field empty for a normally trusted certificate. It uses Desktop's
bundled Node (22.19 or newer is required). MCPB does not install the skill:
download the skill ZIP in BamDude, then use Skills → Add → Upload skill.
Installation, built-in Node, separate skill and model calls to capabilities and
guide were verified on Windows with Claude Desktop 4.4212.0 and an isolated
loopback server. Desktop updates, LAN TLS and macOS remain unverified.

Claude Code: extract its plugin ZIP to a permanent folder. In a terminal, run
`claude plugin marketplace add "<extracted-folder>"`, then
`claude plugin install bamdude-ai@bamdude-ai-local`. Supply address/key/optional
CA in the plugin's user configuration. This connector requires Node 22.19 or
newer on that computer. The package includes the canonical skill under `skills/`.

Codex CLI: extract the portable plugin ZIP to a permanent folder, run
`codex plugin marketplace add "<extracted-folder>"`, then
`codex plugin add bamdude-ai@bamdude-ai-local`. The bundled local catalog requires
no published marketplace or hand-written JSON. The plugin supplies the skill.
Portable plugins have no portable secret-entry
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

## Update a local CLI installation

After updating BamDude, download its matching client ZIP. Close the client and
its MCP connection. Keep the previous extracted folder as a backup, then extract
the new ZIP into a fresh folder at the SAME permanent path registered earlier.
Do not merge files into the old folder or add an extra nested directory.

For Codex CLI, refresh the installed local plugin with:

```powershell
codex plugin add bamdude-ai@bamdude-ai-local
codex plugin list --json
```

For Claude Code:

```powershell
claude plugin update bamdude-ai@bamdude-ai-local
claude plugin list --json
```

Check that the installed version matches the new package, then start a NEW
client session. Existing sessions can keep the old skill. Confirm the key/CA
configuration and ask for capabilities and the guide again. Package replacement
does not rotate or revoke the key. These commands were tested on Windows with
Codex CLI 0.166.0 and Claude Code 2.1.314; they are not a Desktop update proof.
`codex plugin marketplace upgrade` refreshes Git catalogs, not this local ZIP.
If you move the permanent folder, register its new path using the install steps.

To disconnect: revoke the key in BamDude, remove the plugin/connector in the
client, then optionally disable MCP. Uninstalling a plugin never revokes a key.

Full instructions and the current support matrix:
https://docs.bamdude.top/addons/ai-assistants/
