#!/usr/bin/env bash
# Run the render-browser probe inside a copy of the INSTALLED bamdude.service:
# same user, hardening, paths and operator drop-ins -- only Type, ExecStart and
# Restart differ. Plan task 14 (R4): `systemd-run --uid` would not carry the
# unit's NoNewPrivileges / ProtectSystem / PrivateTmp / ... and proves nothing
# about the service context.
set -euo pipefail

UNIT=${UNIT:-bamdude.service}
PROBE=bamdude-render-probe.service
OUT_DIR=${1:?usage: probe_as_linux_service.sh <a ReadWritePaths dir of the service>}
PROPS=User,Group,WorkingDirectory,EnvironmentFiles,Environment,NoNewPrivileges,PrivateTmp,ProtectSystem,ProtectHome,ReadWritePaths,ReadOnlyPaths,InaccessiblePaths,AmbientCapabilities,CapabilityBoundingSet,RestrictNamespaces,SystemCallFilter,SystemCallArchitectures,MemoryDenyWriteExecute,PrivateDevices,PrivateNetwork,ProtectKernelTunables,ProtectKernelModules,ProtectControlGroups,LockPersonality,RestrictSUIDSGID,RestrictRealtime,LimitNOFILE,TasksMax,MemoryMax,KillMode

workdir=$(systemctl show -p WorkingDirectory --value "$UNIT")
user=$(systemctl show -p User --value "$UNIT")
install -d -o "$user" "$OUT_DIR"

tmp=$(mktemp)
# `systemctl cat` = the unit plus every drop-in, in effective order (repeated
# [Service] sections are legal). Keep all of it; replace only how the process
# is started and restarted.
systemctl cat "$UNIT" \
  | grep -vE '^(ExecStart|Type|Restart)=' > "$tmp"
printf '\n[Service]\nType=oneshot\nRestart=no\nExecStart=%s/venv/bin/python -m backend.app.render_browser_probe all --report %s/linux-service.json --keep-netlog %s/linux-service-net.json\n' \
  "$workdir" "$OUT_DIR" "$OUT_DIR" >> "$tmp"

install -m 0644 "$tmp" "/run/systemd/system/$PROBE"
trap 'rm -f "/run/systemd/system/$PROBE"; systemctl daemon-reload' EXIT
systemctl daemon-reload

set +e
systemctl start "$PROBE"   # oneshot: returns when the probe has finished
started=$?
set -e

systemctl show -p "$PROPS" "$UNIT" > "$OUT_DIR/unit-properties.txt"
systemctl show -p "$PROPS" "$PROBE" > "$OUT_DIR/probe-properties.txt"
cp "$tmp" "$OUT_DIR/probe.service"
journalctl -u "$PROBE" --no-pager -n 200 > "$OUT_DIR/probe-journal.txt" || true
if ! diff -u "$OUT_DIR/unit-properties.txt" "$OUT_DIR/probe-properties.txt" > "$OUT_DIR/properties.diff"; then
  echo "effective properties differ -- not a proven-native run; see $OUT_DIR/properties.diff" >&2
  exit 2
fi
echo "probe main status: $(systemctl show -p ExecMainStatus --value "$PROBE") (0 pass, 1 fail, 3 inconclusive)"
exit "$started"
