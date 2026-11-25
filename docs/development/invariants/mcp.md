# Invariants: MCP

Full text of the rules that [`CLAUDE.md`](../../../CLAUDE.md) keeps in short form, with the reasons and the history behind them. `Vault:` pointers name notes in the maintainer's private knowledge base; everything a contributor needs is here.

## MCP authority belongs to one HTTP request

The exact `/mcp` dispatcher is opt-in and key-only; it resolves the existing active owner/REST permission ceiling into a frozen scalar `DiagnosticAccess` in that request's ASGI scope. Old keys receive neither MCP nor MCP-log access. The SDK owns the protocol and stateless manager, whose context is entered and exited by the same owner task; disable/restore close admission and drain MCP-owned auth work even after its requester is cancelled. A restore hold cannot be reopened by a concurrent settings reload. A process-memory MCP observation, never REST `last_used`, is the connection marker; it carries no payload and is exposed only to key managers. The base module offers capabilities and the canonical guide, with no printer commands or diagnostic stubs. The local SDK connector in `integrations/mcp-adapter/` uses one explicit endpoint, client-supplied credentials and verified TLS (an explicit CA for self-signed servers); it refuses all redirects/OAuth discovery and cancels owned HTTP requests on stdio cancellation/EOF. Generated client packages stay build outputs; `integrations/ai-plugins/build.mjs` derives them from the canonical skill and connector. Operator downloads allow only known package IDs and check version, contracts and source/file hashes; a package download or ready runtime never proves a client connection or skill installation. Vault: `40-invariants/inv-mcp-authority-is-request-local.md`.

## MCP current-job identity is not the monitor's display choice

MCP current-job identity is not the monitor's display choice. Conflicting printing rows, a broken header link or a displayed item that disagrees with the header leave `current_job` unknown and produce a diagnostic warning; no reader chooses a replacement physical run or changes the normal monitor projection. Vault: `40-invariants/inv-mcp-authority-is-request-local.md`.

## MCP period totals apply printer scope before aggregation

MCP period totals apply printer scope before SQL aggregation and paging, using normal statistics outcome predicates and created-at selection. Full-selection totals repeat, never become page sums, and remain separate from bounded printer details. Synthetic ends are estimates, unknown energy/cost is not zero, and overlapping selected intervals do not establish utilization, idle time or operator performance. Vault: `40-invariants/inv-mcp-authority-is-request-local.md`.

## MCP diagnostic history correlates IDs, never filenames

MCP diagnostic history correlates archive/job IDs, never filenames, and applies the shared stale-refill policy before time/page filters. Log redaction precedes search and never falls back to raw text. Short secret-collection DB work remains owned through cancellation and restore drain; file-only I/O holds one process-wide slot until its thread really ends, without a database session or a restore dependency. SDK replacement never creates another file reader. Energy reads preserve archive-owned provenance, omit current-plug history before its confirmed source cutoff, and keep page totals separate from sampled counter windows; they never poll, integrate or backfill accounting. Recovery explanations use the normal policy without confirmation, signing or execution; addressed UI links recheck state and browser rights before an operator requests a fresh preview. Vault: `40-invariants/inv-mcp-authority-is-request-local.md`.

## MCP HTTP setup is an explicit transport-risk opt-in, not an authority change

HTTPS remains the default outside loopback. Enabling HTTP requires both request-only acknowledgements; local connectors require their own opt-in and keep HTTPS verification enabled. Settings update and reset serialize policy reads, validation and writes in one database transaction, including when no setting row exists, so a concurrent stale save cannot undo revocation. SDK Host/Origin allowlists stay aligned with the outer gate while DNS rebinding protection remains enabled. Vault: `40-invariants/inv-mcp-authority-is-request-local.md`.
