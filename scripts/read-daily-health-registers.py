#!/usr/bin/env python3
"""Read-only probe for TODO.md item 13's daily-tier registers, before any of them ship.

    sudo docker compose stop dispatch      # REQUIRED first -- see below
    python3 scripts/read-daily-health-registers.py --ip "$INVERTER_IP"
    sudo docker compose start dispatch

WHY THIS EXISTS. `dispatch/registers.py` opens by describing the one time an address was
translated from memory and produced 0x0883 (reactive power) for the mode register. Item 13's
four fields -- SoH, lifetime charge/discharge/grid-charge energy, inverter heatsink
temperature and PV energy -- come from AlphaESS's own "Parameter address table", which is
better provenance than the handover's guesses it corrects, but it is still a document rather
than an observation. The module's rule is that an address ships only after a live read, and
the usual cross-check does not exist here: the AlphaESS app, checked 2026-08-28, surfaces
none of these four. So plausibility is the whole of the evidence, and this script is built to
produce it.

WHAT IT CANNOT TELL YOU. Nothing here confirms a SCALE by itself. A lifetime energy counter
reading 251,394 is 25,139 kWh at 0.1 kWh/bit and 251 MWh at 1 kWh/bit, and both are "a
number". That is why every candidate is printed at several scales and why the two-pass delta
matters more than either absolute value: what a real counter does is go UP, by an amount the
site could actually have moved in the gap.

READ-ONLY. This never calls write_registers. It cannot make anything worse.

THE ONE CONSTRAINT, same as scheduler.py and poll-inverter-diagnostic.py: the inverter
accepts exactly ONE Modbus TCP connection. `sudo docker compose stop dispatch` must COMPLETE
before this runs, or this script takes the dispatcher's connection away instead of failing
cleanly. The dispatcher is blind while it is stopped, so keep --interval short enough to be
comfortable and remember to start it again.

READS WHOLE NEIGHBOURHOODS, not just the four candidate addresses, and that is the point of
the layout below. Item 13's corrections are all off-by-one against the handover, so what
settles them is the SHAPE of the surrounding words -- a lifetime energy block is three
increasing pairs followed by a gap followed by 0x0126, which this repo has already confirmed
live as battery power. Reading only 0x011F would show a number; reading 0x011F-0x0126 shows
whether it is the number in the right place.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dispatch"))
import registers as R  # noqa: E402

# (start, count, label). Chosen to bracket each candidate with words whose role is already
# known, so a boundary can be read off the output rather than assumed.
WINDOWS = [
    (0x0115, 0x0128 - 0x0115 + 1, "firmware / SoH / lifetime energy / battery power"),
    (0x0430, 0x0440 - 0x0430 + 1, "inverter temperature / inverter faults / inverter PV"),
    (0x08D0, 5, "system lifetime PV energy"),
]

# The four candidates, as (label, address, word count, kind). `kind` only selects which
# scale hypotheses get printed -- nothing here is treated as confirmed.
CANDIDATES = [
    ("SoH",                        0x011B, 1, "percent"),
    ("lifetime charge energy",     0x011F, 2, "energy"),
    ("lifetime discharge energy",  0x0121, 2, "energy"),
    ("lifetime grid-charge energy", 0x0123, 2, "energy"),
    ("inverter heatsink temp",     0x0434, 1, "temp"),
    ("inverter lifetime PV energy", 0x043D, 2, "energy"),
    ("system lifetime PV energy",  0x08D1, 2, "energy"),
]

# Already confirmed live on this site. Read alongside, so a run that produces nonsense
# everywhere is distinguishable from a run against a working connection.
ANCHORS = [
    ("battery power (0x0126, confirmed)", R.REG_BATTERY_POWER, 1, True),
    ("battery SoC (0x0102, confirmed)", R.REG_BATTERY_SOC, 1, False),
]

SCALES = {
    "percent": [("1 %/bit", 1.0), ("0.1 %/bit", 0.1)],
    "temp": [("1 C/bit", 1.0), ("0.1 C/bit", 0.1)],
    "energy": [("1 kWh/bit", 1.0), ("0.1 kWh/bit", 0.1),
               ("0.01 kWh/bit", 0.01), ("1 Wh/bit", 0.001)],
}


def read_window(client, kw, slave, addr, count):
    r = client.read_holding_registers(addr, **{"count": count, kw: slave})
    if r.isError():
        raise OSError(f"read {addr:#06x}+{count} failed: {r}")
    return list(r.registers)


def sample(client, kw, slave) -> dict:
    """One pass: every window, keyed by base address."""
    return {addr: read_window(client, kw, slave, addr, count)
            for addr, count, _ in WINDOWS}


def word_at(pass_, addr, count):
    """Pull `count` words at `addr` out of whichever window contains them."""
    for base, words in pass_.items():
        if base <= addr and addr + count - 1 < base + len(words):
            return words[addr - base:addr - base + count]
    raise KeyError(f"{addr:#06x} is not inside any window")


def print_windows(pass_):
    for base, count, label in WINDOWS:
        words = pass_[base]
        print(f"\n  {base:#06x}-{base + count - 1:#06x}  {label}")
        for i in range(0, len(words), 8):
            chunk = words[i:i + 8]
            addrs = f"{base + i:#06x}:"
            print(f"    {addrs:>10} " + " ".join(f"{w:5d}" for w in chunk)
                  + "   |  " + " ".join(f"{w:04x}" for w in chunk))


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    # No hard-coded default. `.env.example` and docker-compose.yml still carry
    # 192.168.68.151 from the old subnet (TODO.md item 18's first half), and a probe that
    # silently aims at a dead address after you stopped the dispatcher for it is a waste of
    # the one window you get.
    p.add_argument("--ip", default=os.environ.get("INVERTER_IP", ""),
                   help="defaults to $INVERTER_IP; take it from the NAS .env")
    p.add_argument("--port", type=int, default=502)
    p.add_argument("--slave-id", type=int, default=0x55)
    p.add_argument("--interval", type=float, default=180.0,
                   help="seconds between the two passes (monotonicity check)")
    args = p.parse_args()

    if not args.ip:
        print("ERROR: no --ip and no INVERTER_IP in the environment. Read it out of the "
              "NAS .env:\n"
              "  INVERTER_IP=$(sed -n 's/^INVERTER_IP=//p' .env | head -1 | tr -d \"\\\"'\")",
              file=sys.stderr)
        sys.exit(2)

    from pymodbus.client import ModbusTcpClient
    import inspect
    kw = "device_id" if "device_id" in inspect.signature(
        ModbusTcpClient.read_holding_registers).parameters else "slave"

    client = ModbusTcpClient(args.ip, port=args.port)
    if not client.connect():
        print(f"ERROR: could not connect to {args.ip}:{args.port}. Is `dispatch` really "
              f"stopped? (`sudo docker compose ps dispatch`)", file=sys.stderr)
        sys.exit(1)

    print(f"Connected to {args.ip}:{args.port}, slave {args.slave_id:#x}. READ ONLY.")
    print(f"Two passes {args.interval:.0f}s apart, so a counter can be seen to count.\n")

    try:
        for label, addr, count, signed in ANCHORS:
            words = read_window(client, kw, args.slave_id, addr, count)
            print(f"  anchor  {label:<38} raw={R.decode(words, signed)}")

        print(f"\n=== PASS 1  {dt.datetime.now().isoformat(timespec='seconds')} ===")
        first = sample(client, kw, args.slave_id)
        print_windows(first)

        print(f"\nsleeping {args.interval:.0f}s ...", flush=True)
        time.sleep(args.interval)

        print(f"\n=== PASS 2  {dt.datetime.now().isoformat(timespec='seconds')} ===")
        second = sample(client, kw, args.slave_id)
        print_windows(second)
    finally:
        client.close()

    print("\n=== CANDIDATES ===")
    print("Nothing below is confirmed. Read the deltas first: a lifetime counter must not "
          "go down,\nand must not move by more than this site could have produced in "
          f"{args.interval:.0f}s.\n")
    for label, addr, count, kind in CANDIDATES:
        a = R.decode(word_at(first, addr, count))
        b = R.decode(word_at(second, addr, count))
        print(f"  {label}  ({addr:#06x}, {count} word{'s' if count > 1 else ''})")
        print(f"    raw  pass1={a}  pass2={b}  delta={b - a:+d}")
        for name, scale in SCALES[kind]:
            print(f"    {name:<14} {a * scale:14.3f} -> {b * scale:14.3f}"
                  f"   delta {(b - a) * scale:+.3f}")
        print()

    print("Sanity checks worth doing by eye, none of which need a bit map:")
    print("  * SoH should sit just under 100 %, not at 0 and not in the thousands.")
    print("  * discharge < charge, and grid-charge <= charge: a round trip loses energy,")
    print("    and grid charging is a subset of all charging.")
    print("  * discharge/charge should land near 0.90-0.96. That ratio is scale-free, so it")
    print("    is the one check that survives not knowing the units.")
    print("  * heatsink temp should be above ambient and below ~60 C while inverting.")
    print("  * every lifetime counter must be >= what this repo has already recorded for")
    print("    the period it has been running -- compare against daily_energy in InfluxDB.")
    print("\nRemember: sudo docker compose start dispatch")


if __name__ == "__main__":
    main()
