# Virtual printer startup cleanup — validation

## Scope

Non-proxy VP startup now waits for every configured listener, including the
camera passthrough when present. A child exiting before readiness fails the
attempt immediately; a stalled child remains bounded by the startup deadline.
Failed or cancelled startup stops the partial instance. The real-printer MQTT
bridge attaches after listener readiness and is detached on any later startup
failure. Neither the instance nor its manager logs a successful start on failure.
No schema, frontend, or proxy-mode lifecycle changes.

The documentation site's English and Ukrainian Virtual Printer pages explain
local-address ownership, one IP per simultaneous VP, native Windows aliases,
DHCP precautions, address removal, and Windows native versus Docker Desktop.
One VP can use the host's primary LAN IP when its ports are free; extra aliases
are needed when there are not enough distinct suitable local addresses.

## Checks run

- `python -m ruff check backend/` — passed.
- Ruff formatting check on the four modified Python files — passed after formatting.
- All `test_vp_*` / `test_virtual_printer` unit files plus
  `backend/tests/integration/test_virtual_printer_api.py` — **344 passed**.
- Documentation repository: `python -m mkdocs build --strict` — both languages passed.
- PowerShell parser: all four fenced command blocks per language parsed without errors.
- `git diff --check` — passed in both repositories.

Regression cases include a swallowed bind error, a raised child error, a child
exiting after signalling readiness, stalled camera startup, cancelled startup,
service-construction failure, bridge failure after callback attachment, restart
after correction, and continuing to start another VP after the first fails.
Real loopback TCP tests verify listener readiness/cleanup and an occupied port.

## Limits

No physical printer, farm server, network adapter, DHCP setting or firewall was
modified. Windows alias commands were checked against Microsoft documentation
and parsed locally; they were not executed against a real adapter. These checks
do not establish the cause of historical REST/WebSocket latency. Existing failed
VPs can be retried by toggling the affected VP off and on after correcting its
local address or port conflict.
