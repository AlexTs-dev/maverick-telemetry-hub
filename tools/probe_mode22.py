#!/usr/bin/env python3
"""
tools/probe_mode22.py
Maverick Telemetry Hub — manual Ford Mode 22 (UDS ReadDataByIdentifier) probe.

A hand-run diagnostic for discovering / re-validating proprietary BECM PIDs on
the OBDLink EX. NOT part of the runtime: it touches no MQTT, SQLite, or systemd.
It opens its own serial connection, queries one DID at a chosen rate, and prints
the raw `62 …` response plus several candidate scalings so you can correlate a
value against the dash. Read-only — it only ever sends UDS service 0x22 reads.

The verified production PIDs (DID 4801 SOC, 4808 temp, 480D voltage on header
7E4) live in obd_poller.py. Use this tool to confirm them on a new car, refine a
scaling, or hunt the next signal — e.g. HV current near 48FB: watch the signed
value flip sign on hard accel (discharge) vs strong regen (charge).

Requires: pip install obd   (the car must be plugged in, ignition in "Ready").

Examples:
    python tools/probe_mode22.py                      # SOC (7E4 / 4801) at 1 Hz
    python tools/probe_mode22.py --did 4808           # pack temperatures
    python tools/probe_mode22.py --did 48FB --signed  # candidate HV current
    python tools/probe_mode22.py --header 7E0 --did F190 --port COM5 --once
"""

import argparse
import sys
import time

import obd
from obd.protocols import ECU


# Known DIDs for nicer labels (purely cosmetic). See PORTABLE_SPEC.md §3/§6.
KNOWN = {
    "4801": "HV SOC       (A*256+B)/500 = %",
    "4808": "Pack temps   A/B/C/D = max/min/range/avg, raw-50 = °C",
    "480D": "HV voltage   (A*256+B)/100 = V",
    "4800": "HvbTemp      A-50 = °C",
    "48FB": "HV current?  verify sign on charge vs discharge",
    "F190": "VIN          (UDS, any module that knows it)",
}


def build_args():
    p = argparse.ArgumentParser(
        description="Probe a Ford Mode 22 DID and print the raw response + candidate scalings."
    )
    p.add_argument("--did",    default="4801", help="2-byte DID hex, e.g. 4801 (default: SOC)")
    p.add_argument("--header", default="7E4",  help="CAN request header (default: 7E4 = BECM)")
    p.add_argument("--port",   default=None,   help="serial port, e.g. COM5 or /dev/ttyUSB0 (default: auto)")
    p.add_argument("--baud",   type=int,   default=115200, help="adapter baud (default: 115200)")
    p.add_argument("--hz",     type=float, default=1.0,    help="poll rate in Hz (default: 1.0)")
    p.add_argument("--signed", action="store_true", help="also show signed (two's complement) u16")
    p.add_argument("--once",   action="store_true", help="query once and exit")
    return p.parse_args()


def make_command(header: str, did: str) -> obd.OBDCommand:
    """A custom Mode 22 command whose decoder returns the raw UDS payload bytes."""
    request = f"22{did}".encode("ascii")
    return obd.OBDCommand(
        f"PROBE_{did}", f"probe {did}", request, 0,
        lambda m: bytes(m[0].data) if m else None,
        ecu=ECU.ALL, fast=False, header=header.encode("ascii"),
    )


def s16(u: int) -> int:
    return u - 0x10000 if u >= 0x8000 else u


def describe(data: bytes, signed: bool) -> str:
    """Render candidate scalings from the data bytes (after the `62 DID` echo)."""
    if not data:
        return "(no data bytes)"
    parts = [f"raw={data.hex(' ')}"]
    a = data[0]
    parts.append(f"A={a} (A-50={a - 50}°C/{(a - 50) * 1.8 + 32:.0f}°F, A·100/255={a * 100 / 255:.1f}%)")
    if len(data) >= 2:
        u16 = data[0] * 256 + data[1]
        parts.append(f"u16={u16} (/500={u16 / 500:.2f}%, /100={u16 / 100:.1f}V, ×0.0039={u16 * 0.00390625:.2f})")
        if signed:
            parts.append(f"s16={s16(u16)}")
    if len(data) >= 4:
        d = data[3]
        parts.append(f"D={d} (D-50={d - 50}°C/{(d - 50) * 1.8 + 32:.0f}°F)")
    return "  ".join(parts)


def main() -> None:
    args = build_args()
    cmd  = make_command(args.header, args.did)

    label = KNOWN.get(args.did.upper(), "unmapped")
    print(f"Probing header {args.header} DID {args.did} — {label}")
    print(f"Connecting to {args.port or 'auto-detected port'} @ {args.baud} ...")

    conn = obd.OBD(
        args.port, baudrate=args.baud, protocol="6",
        fast=False, timeout=1.0, check_voltage=False,
    )
    if not conn.is_connected():
        print("ERROR: no OBD connection. Is the adapter plugged into the car with the key ON?",
              file=sys.stderr)
        sys.exit(1)
    print("Connected. Ctrl-C to stop.\n")

    interval = 1.0 / args.hz if args.hz > 0 else 1.0
    try:
        while True:
            resp = conn.query(cmd, force=True)
            raw  = resp.value if not resp.is_null() else None
            if not raw:
                print("  no response (silent — wrong header/module, or DID absent)")
            elif raw[0] == 0x7F:
                nrc = raw[2] if len(raw) >= 3 else 0
                print(f"  negative response {raw.hex(' ')} (NRC {nrc:#04x} — out of range / unsupported)")
            elif raw[0] == 0x62:
                print("  " + describe(raw[3:], args.signed))
            else:
                print(f"  unexpected: {raw.hex(' ')}")
            if args.once:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
