#!/usr/bin/env python3
"""Watch a Bambu printer's MQTT traffic from outside BamDude.

Opens its **own** session to the printer and prints what crosses it. The job it
exists for is protocol archaeology: seeing the exact payload BambuStudio or
OrcaSlicer sends for something BamDude does not implement yet, on a machine
BamDude may not even know about.

⚠️ **For a printer BamDude already manages, use the in-app recorder instead.**
Printers page -> the printer's card -> *Record MQTT*. It tees the connection
BamDude already holds, writes to a file, survives a closed terminal, and shows
a badge so a forgotten recording is visible. This script cannot do any of that,
and a **second session to a printer that is already connected is what made
generating a support bundle disturb the whole farm** (see the branch-order note
in ``printer_diagnostic``). Point it at a printer BamDude is NOT connected to.

What it is still the only answer for:

* a printer that is not in BamDude at all --- a machine on the bench, or one you
  are evaluating before adding;
* a connection that never establishes, so there is nothing for the recorder to
  tee: wrong access code, wrong serial, TLS refused;
* watching what *another client* sends while it drives the printer.

Usage:
    python scripts/mqtt_sniffer.py <printer_ip> <serial_number> [--all] [--topic T]

The access code is read from ``BAMBU_ACCESS_CODE`` or prompted for, never taken
as an argument --- an argument lands in shell history, in ``ps`` output and in
whatever terminal log or screenshot the session ends up in.

Example:
    export BAMBU_ACCESS_CODE=...      # or just let it prompt
    python scripts/mqtt_sniffer.py 192.168.1.100 0948BB540200427
"""

import argparse
import getpass
import json
import os
import ssl
import sys
from datetime import datetime

import paho.mqtt.client as mqtt

# ``push_status`` is the printer's continuous telemetry — several a second,
# and never the thing being hunted. Hidden by default, shown with --all.
NOISY_COMMANDS = {"push_status"}


def _stamp(with_millis: bool = False) -> str:
    now = datetime.now()
    return now.strftime("%H:%M:%S.%f")[:-3] if with_millis else now.strftime("%H:%M:%S")


def on_connect(client, userdata, flags, rc, properties=None):
    """paho CallbackAPIVersion.VERSION2 — same signature the app's client uses."""
    if rc != 0:
        print(f"[{_stamp()}] Connection failed: {rc}")
        return
    topic = userdata["topic"]
    print(f"[{_stamp()}] Connected to printer.")
    client.subscribe(topic)
    print(f"[{_stamp()}] Subscribed to: {topic}")
    print("-" * 80)
    print("Listening. Ctrl+C to stop.")
    if userdata["highlight"]:
        print(f"Payloads whose command contains {userdata['highlight']!r} are printed in full.")
    print("-" * 80)


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode("utf-8"))
    except json.JSONDecodeError:
        print(f"[{_stamp()}] Non-JSON message on {msg.topic} ({len(msg.payload)} bytes)")
        return
    except Exception as e:  # noqa: BLE001 - a sniffer must not die on one odd frame
        print(f"[{_stamp()}] Error reading message: {e}")
        return

    command = ""
    if isinstance(payload.get("print"), dict):
        command = payload["print"].get("command", "") or ""

    highlight = userdata["highlight"]
    if highlight and highlight in command:
        print(f"\n{'=' * 80}")
        print(f"[{_stamp(True)}] *** {command} ***")
        print(f"Topic: {msg.topic}")
        print(json.dumps(payload, indent=2))
        print(f"{'=' * 80}\n")
        return

    if userdata["show_all"]:
        print(f"\n[{_stamp(True)}] {msg.topic} — {command or '(no command)'}")
        print(json.dumps(payload, indent=2))
        return

    if command and command not in NOISY_COMMANDS:
        print(f"[{_stamp()}] Command: {command}")


def on_disconnect(client, userdata, disconnect_flags=None, rc=None, properties=None):
    print(f"[{_stamp()}] Disconnected: {rc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="mqtt_sniffer",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("printer_ip")
    parser.add_argument("serial_number")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Print every message in full, telemetry included (very noisy).",
    )
    parser.add_argument(
        "--highlight",
        metavar="SUBSTRING",
        default="extrusion_cali",
        help="Print the full payload of commands containing this (default: extrusion_cali). "
        "Pass an empty string to highlight nothing.",
    )
    parser.add_argument(
        "--topic",
        help="Topic to subscribe to (default: device/<serial>/report).",
    )
    args = parser.parse_args(argv)

    access_code = os.environ.get("BAMBU_ACCESS_CODE") or getpass.getpass("Printer access code: ")
    if not access_code:
        print("No access code given.", file=sys.stderr)
        return 2

    topic = args.topic or f"device/{args.serial_number}/report"
    print(f"Connecting to {args.printer_ip} (serial {args.serial_number})...")

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        protocol=mqtt.MQTTv311,
        userdata={"topic": topic, "show_all": args.all, "highlight": args.highlight},
    )
    client.username_pw_set("bblp", access_code)

    # Bambu's broker presents a self-signed certificate; the app's own client
    # does the same thing for the same reason.
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    client.tls_set_context(ssl_context)

    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = on_disconnect

    try:
        client.connect(args.printer_ip, 8883, 60)
        client.loop_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
        client.disconnect()
    except Exception as e:  # noqa: BLE001 - report the reason, do not traceback at an operator
        print(f"Error: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
