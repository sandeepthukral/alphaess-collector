#!/usr/bin/env python3
"""Is the dispatcher deciding? One screen, from the Mac, no SSH and no Modbus.

    scripts/is-it-deciding.py
    scripts/is-it-deciding.py --watch          # re-reads every 60 s until Ctrl-C

WHY THIS EXISTS. In dry run the dashboard cannot answer this question. Every dispatch panel
on `alphaess-battery-plan` reads back the INVERTER's registers, and dry run never writes
them -- so `Dispatch state` reads `no dispatch`, `Commanded power` reads 0 W and the decoded
register table reads `Released` on every tick of the day, whatever the dispatcher decided.
Watching those panels through a dry-run day tells you nothing, which is the trap this script
exists to get you out of. The decision is carried by `slot_action`, published by `state.py`
from the SLOT rather than from the registers, for exactly this reason.

`review-dry-run.py` answers the same question over a whole day and is the one that gates
going live. This answers it for RIGHT NOW, in under a second, which is what you want when the
question is "did I just break it" rather than "was yesterday any good".

THE THREE CHECKS, AND WHY THIS IS THE ONLY ONE THAT RUNS FROM HERE.

  1. this script                -- reads Influx. No credentials on the NAS, cannot touch the
                                   inverter, works from anywhere on the tailnet.
  2. `scheduler.py --alive`     -- reads the local heartbeat file inside the container. Needs
                                   `sudo docker compose exec` on the Synology.
  3. `review-dry-run.py`        -- the whole day, including the gaps the other two cannot
                                   see: a loop that died and restarted looks perfectly
                                   healthy to both of them a minute later.

Read-only, and only the `alphaess` bucket.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from review_page import local, parse_ts  # noqa: E402

# The dispatcher's loop interval, and the silence that means it stopped. Both deliberately
# the same numbers `review-dry-run.py` uses -- the two scripts disagreeing about what counts
# as a stalled loop would be worse than either of them being slightly wrong.
TICK_S = 60
GAP_S = 180
# The translator runs every three hours; past this the plan is one it should already have
# replaced. Monitor #3's window in DESIGN-dispatch.md section 6.1.
STALE_PLAN_S = 4 * 3600

FIELDS = ("slot_action", "action", "plan_run", "setpoint_w", "dispatch_active")

# What each decision means, in the words the dispatcher would use if asked. `self` is the
# one that reads wrong at a glance: it is not "no decision", it is the deliberate RELEASE of
# dispatch, and section 4.1 makes that a decision like any other.
MEANS = {
    "charge": "forced charge -- Mode 2, power and SoC target written",
    "discharge": "forced discharge -- Mode 2, power and SoC target written",
    "hold": "battery frozen at 0 W -- Mode 3, surplus exported",
    "self": "dispatch RELEASED on purpose -- plain self-consumption",
}


def read_state(api, bucket: str):
    """The newest `dispatch_state` point, as a plain dict, or `None`.

    A FIVE MINUTE WINDOW, for the reason panel 20 documents at length: a dead dispatcher does
    not clear `dispatch_state`, it leaves its last point sitting there forever. Query a wide
    range with `last()` and you get a confident, well-formatted answer describing a dispatcher
    that stopped during breakfast. No rows at all is the honest reply, so the window is what
    enforces it -- not the age arithmetic below, which never runs if the point is old enough.
    """
    flux = f'''from(bucket: "{bucket}")
  |> range(start: -5m)
  |> filter(fn: (r) => r._measurement == "dispatch_state")
  |> filter(fn: (r) => contains(value: r._field, set: {list(FIELDS)!r}))
  |> last()
  |> keep(columns: ["_time", "_field", "_value"])'''.replace("'", '"')

    out: dict[str, object] = {}
    for table in api.query(flux):
        for rec in table.records:
            out[rec.get_field()] = rec.get_value()
            out["_time"] = rec.get_time()
    return out or None


def report(state: dict | None) -> int:
    """Print the verdict. Returns the exit code, so `--watch` and a shell both agree."""
    now = dt.datetime.now(dt.UTC)

    if state is None:
        print("NOT DECIDING -- no dispatch_state point in the last 5 minutes.")
        print("  The loop is down, or it cannot reach InfluxDB. On the NAS:")
        print("    sudo docker compose logs --tail 50 dispatch")
        return 2

    age = (now - state["_time"]).total_seconds()
    decided = state.get("slot_action")
    live = state.get("dispatch_active") not in (0, None)

    # `slot_action` IS A CONDITIONAL FIELD and its absence is not a fault. `state.py:113`
    # writes it only while a slot is active, so a healthy dispatcher outside the plan's
    # horizon -- or in a gap between slots -- writes a point with no `slot_action` at all.
    # Reporting that as trouble would cry wolf on a normal resting state, the same mistake
    # panel 23 made with `expires_at` before it was gated. The dead-dispatcher case is the
    # `state is None` branch above, and only that branch.
    if decided:
        print(f"  decision   {decided:<10s} {MEANS.get(str(decided), '')}")
    else:
        print("  decision   (no slot)   nothing planned for right now -- normal outside "
              "the horizon")

    mode = "live" if live else "dry run"
    tail = "" if live else "   <- dry run writes nothing, so this never changes"
    print(f"  readback   {state.get('action')} / {state.get('setpoint_w')} W "
          f"({mode}){tail}")

    if state.get("plan_run"):
        plan_age = (now - parse_ts(str(state["plan_run"]))).total_seconds()
        flag = "  STALE, the translator has stopped" if plan_age > STALE_PLAN_S else ""
        print(f"  plan       {state['plan_run']}  ({plan_age / 3600:.1f} h old){flag}")

    print(f"  last tick  {local(state['_time']):%H:%M:%S}  ({age:.0f} s ago)")

    if age > GAP_S:
        print(f"\nSTALLED -- {age:.0f} s since the last tick, over the {GAP_S} s threshold.")
        return 1
    print(f"\nDECIDING -- ticking every {TICK_S} s as it should.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--url", default=os.environ.get("INFLUX_URL", "http://192.168.68.105:8086"))
    p.add_argument("--org", default=os.environ.get("INFLUX_ORG", "home"))
    p.add_argument("--bucket", default="alphaess")
    p.add_argument("--token-env", default="INFLUX_TOKEN_GRAFANA",
                   help="env var holding the token; read-only is enough")
    p.add_argument("--watch", nargs="?", type=float, const=float(TICK_S), default=None,
                   metavar="SECONDS", help=f"re-read forever, default every {TICK_S} s")
    a = p.parse_args()

    token = os.environ.get(a.token_env)
    if not token:
        sys.exit(f"{a.token_env} is not set -- export it or pass --token-env")

    from influxdb_client import InfluxDBClient

    with InfluxDBClient(url=a.url, token=token, org=a.org) as client:
        api = client.query_api()
        if a.watch is None:
            return report(read_state(api, a.bucket))
        # Line buffering, because stdout to anything but a terminal is block buffered and a
        # watch whose output only appears when it is killed is not a watch. `tee`ing this to
        # a file overnight is the obvious thing to do with it.
        sys.stdout.reconfigure(line_buffering=True)
        # Deliberately does not clear the screen: the point of watching is to see the
        # decision CHANGE at a slot boundary, and a scrollback of stamped readings shows that
        # where a repainted single frame hides it.
        while True:
            print(f"--- {local(dt.datetime.now(dt.UTC)):%H:%M:%S} "
                  f"-------------------------------------------")
            report(read_state(api, a.bucket))
            time.sleep(a.watch)


if __name__ == "__main__":
    raise SystemExit(main())
