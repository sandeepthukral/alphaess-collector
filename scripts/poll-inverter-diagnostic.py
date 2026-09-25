#!/usr/bin/env python3
"""One-off diagnostic poll of the inverter's dispatch and measurement registers.

    sudo docker compose stop dispatch      # REQUIRED first -- see below
    python3 scripts/poll-inverter-diagnostic.py --ip 192.168.68.151

On the NAS, where only the dispatch image has pymodbus, run it in that image with the
script and registers.py mounted in (--ip and --p1-url default to $INVERTER_IP and
$P1_MONITOR_URL):

    sudo docker compose run --rm --no-deps -v "$PWD/scripts:/x/scripts:ro" \
      -v "$PWD/dispatch/registers.py:/x/dispatch/registers.py:ro" \
      -e INVERTER_IP -e P1_MONITOR_URL --entrypoint python dispatch \
      /x/scripts/poll-inverter-diagnostic.py --out /tmp/poll.csv --duration 600

Built to chase a specific mismatch: the app commands a full discharge (e.g. 4700 W) and the
battery only appears to deliver ~4450 W. This reads the dispatch block (what is COMMANDED,
whoever wrote it -- the app writes the same registers dispatch.py does, see
dispatch/registers.py:213) alongside the live measurement registers (what is ACTUALLY
happening) every `--interval` seconds for `--duration` seconds, and writes both to a CSV.

READ-ONLY. This never calls write_registers. It cannot make the problem it is diagnosing
worse.

THE ONE CONSTRAINT, same as scheduler.py: the inverter accepts exactly ONE Modbus TCP
connection. `sudo docker compose stop dispatch` MUST complete before this runs, or this
script's connection attempt takes the dispatcher's connection away instead of failing
cleanly. Nothing else may hold :502 for the run's duration.

NOTE ON PROVENANCE: the Grafana/InfluxDB "achieved power" figure this script exists to
explain comes from collector.py, which polls the AlphaESS CLOUD API (getLastPowerData's
`pbat` field) -- not this register. This script reads 0x0126 directly off the local Modbus
connection, which is a third, more authoritative data path. Do not assume the two are sampled
the same way; a gap between them proves nothing about the inverter itself.

CT VS P1 (`--p1-url`): also fetches the P1 monitor's whole-house grid power each poll and
prints it beside the inverter's own CT reading (REG_GRID_POWER). Both use the same sign
(positive = importing). Under GRID_SOURCE=p1 a release hands the battery to the inverter's
self-consumption, which steers on that CT alone -- and has been seen to discharge into an
export (2026-09-24) and charge from the grid before sunrise (2026-09-25). Reading the two
side by side says which:

    opposite signs              -> the CT is reversed
    same sign, very different   -> the CT sits on a phase that does not carry the load
    roughly equal               -> the CT is fine; look elsewhere

Run it while the inverter is in its own self-consumption (dispatch stopped, app not
force-charging), ideally across both an import and an export.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dispatch"))
import registers as R

FIELDS = [
    "timestamp", "elapsed_s",
    "dispatch_active", "mode", "mode_name",
    "commanded_power_w", "target_soc_pct", "duration_s",
    "battery_power_w", "battery_soc_pct",
    "grid_power_w", "pv_power_w", "load_power_w",
    "max_charge_w", "max_discharge_w",
    "p1_grid_w", "p1_load_w",
]


def fetch_p1_grid_w(url: str) -> float | None:
    """The P1 monitor's grid power, positive = importing -- same body shape and sign as
    dispatch/scheduler.py's fetch_p1_grid_w. None on any failure: a missed P1 sample must
    not cost the Modbus row it sits beside."""
    try:
        with urlopen(url, timeout=5) as resp:
            return float(json.load(resp)["active_power_w"])
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"  P1 fetch failed: {e}", file=sys.stderr)
        return None


def poll_once(client, slave_id: int, kw: str) -> dict:
    def read(addr, count=1, signed=False):
        r = client.read_holding_registers(addr, **{"count": count, kw: slave_id})
        if r.isError():
            raise OSError(f"read {addr} failed: {r}")
        return R.decode(r.registers, signed)

    addr, count = R.DISPATCH_BLOCK
    r = client.read_holding_registers(addr, **{"count": count, kw: slave_id})
    if r.isError():
        raise OSError(f"dispatch block read failed: {r}")
    block = R.decode_block(list(r.registers))

    battery_power_w = read(R.REG_BATTERY_POWER, signed=True)
    grid_power_w = read(R.REG_GRID_POWER, count=2, signed=True)
    pv_power_w = read(R.REG_PV_METER, count=2, signed=True)
    battery_soc_pct = round(read(R.REG_BATTERY_SOC) / 10, 1)
    max_charge_w = read(R.REG_MAX_CHARGE_POWER)
    max_discharge_w = read(R.REG_MAX_DISCHARGE_POWER)

    return {
        "dispatch_active": block["dispatch_active"],
        "mode": block["mode"],
        "mode_name": block["mode_name"],
        "commanded_power_w": block["power_w"],
        "target_soc_pct": block["target_soc_pct"],
        "duration_s": block["duration_s"],
        "battery_power_w": battery_power_w,
        "battery_soc_pct": battery_soc_pct,
        "grid_power_w": grid_power_w,
        "pv_power_w": pv_power_w,
        # pv + grid + battery, charging-positive/discharging-positive conventions per
        # registers.py header. Derived, not measured -- see project memory on load_power_w.
        "load_power_w": pv_power_w + grid_power_w + battery_power_w,
        "max_charge_w": max_charge_w,
        "max_discharge_w": max_discharge_w,
    }


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--ip", default=os.environ.get("INVERTER_IP", "192.168.68.151"))
    p.add_argument("--port", type=int, default=502)
    p.add_argument("--slave-id", type=int, default=0x55)
    p.add_argument("--interval", type=float, default=5.0, help="seconds between polls")
    p.add_argument("--duration", type=float, default=300.0, help="total seconds to run")
    p.add_argument("--out", default=None, help="CSV path (default: timestamped in cwd)")
    p.add_argument("--p1-url", default=os.environ.get("P1_MONITOR_URL", ""),
                   help="P1 monitor URL to read beside the CT (default: $P1_MONITOR_URL; "
                        "empty = skip)")
    args = p.parse_args()

    out_path = Path(args.out) if args.out else Path(
        f"inverter-poll-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.csv")

    import inspect

    from pymodbus.client import ModbusTcpClient
    kw = "device_id" if "device_id" in inspect.signature(
        ModbusTcpClient.read_holding_registers).parameters else "slave"

    client = ModbusTcpClient(args.ip, port=args.port)
    if not client.connect():
        print(f"ERROR: could not connect to {args.ip}:{args.port}. Is `dispatch` really "
              f"stopped? (`sudo docker compose ps dispatch`)", file=sys.stderr)
        sys.exit(1)

    print(f"Connected to {args.ip}:{args.port}, slave {args.slave_id:#x}.")
    print(f"Polling every {args.interval}s for {args.duration}s -> {out_path}")
    print("NOTE: battery_power_w/grid_power_w/pv_power_w are local-Modbus readings, a "
          "different data path from the Grafana figure that came from the cloud API.")

    start = time.monotonic()
    n = 0
    try:
        with out_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=FIELDS)
            writer.writeheader()
            while True:
                elapsed = time.monotonic() - start
                if elapsed >= args.duration:
                    break
                now = dt.datetime.now(dt.UTC).isoformat()
                try:
                    row = poll_once(client, args.slave_id, kw)
                except OSError as e:
                    print(f"  [{elapsed:6.1f}s] read failed: {e}", file=sys.stderr)
                    time.sleep(args.interval)
                    continue
                p1 = fetch_p1_grid_w(args.p1_url) if args.p1_url else None
                row["p1_grid_w"] = p1
                # The whole-house load by the same identity as collector.parse_fields,
                # with P1 in place of the CT -- the figure the CT's own load_power_w
                # should match if the CT saw the whole house.
                row["p1_load_w"] = (None if p1 is None else
                                    row["pv_power_w"] + p1 + row["battery_power_w"])
                row["timestamp"] = now
                row["elapsed_s"] = round(elapsed, 1)
                writer.writerow(row)
                f.flush()
                n += 1
                p1_text = "" if p1 is None else f"  P1={p1:+6.0f}W"
                print(f"  [{elapsed:6.1f}s] mode={row['mode_name']:<12} "
                      f"commanded={row['commanded_power_w']:+6d}W  "
                      f"battery={row['battery_power_w']:+6d}W  "
                      f"soc={row['battery_soc_pct']:.1f}%  "
                      f"CT={row['grid_power_w']:+6d}W{p1_text}  pv={row['pv_power_w']:+6d}W")
                time.sleep(args.interval)
    finally:
        client.close()

    print(f"\nDone. {n} samples written to {out_path}")


if __name__ == "__main__":
    main()
