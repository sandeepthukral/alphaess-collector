"""The 3-hourly translator job. DESIGN-dispatch.md sections 4 and 6.1.

Reads the planner's output from InfluxDB, turns it into `slots.json`, and reports to the two
Kuma monitors that watch this half of the system:

  #2 `plan-in-influx`  -- the plan was made but the Influx write silently failed
  #3 `slots-written`   -- the plan was readable but the translation failed

Those are separate monitors because they have different fixes. #2 down means look at
`battery-planning`; #3 down means look here.

This module is the boundary layer only. Every decision about WHAT the slots should be lives in
`translator.py`, which is pure and golden-tested; everything here is I/O -- a query, a file
write and two HTTP pings. The split is the same one `scheduler.py` keeps against `slots.py`.

WHY THE FILE IS WRITTEN ATOMICALLY. `scheduler.py` re-reads `slots.json` whenever its mtime
changes, on a 60 s loop that may land in the middle of this write. A partial file would parse
as invalid JSON and take the dispatcher idle for as long as it took to notice. Write to a
sibling temp file and rename.

WHY A FAILED RUN LEAVES THE OLD FILE IN PLACE. A stale slots file is a known, monitored,
gracefully-degrading state: `slots.decide()` refuses to dispatch past its freshness window and
monitor #4 says so. An absent or truncated one is not. So nothing here deletes or truncates
the existing file on the way to failing.

Usage:
    python translate.py --slots /data/slots.json            # one run, reads INFLUX_* from env
    python translate.py --slots /data/slots.json --dry-run  # print the summary, write nothing
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path

from heartbeat import send_heartbeat
from plan import PlanFormatError, from_influx, interval_minutes, newest_by_interval
from translator import build_document

log = logging.getLogger("dispatch")

# How far back and forward to query. Back one hour so the interval containing `now` is always
# included even after a slow run; forward two days because the planner's horizon is ~36 h and
# asking for more than exists costs nothing.
LOOKBACK = dt.timedelta(hours=1)
LOOKAHEAD = dt.timedelta(hours=48)

# Nameplate usable capacity. Environment-overridable rather than imported, because
# `battery-planning/hardware.py` is across a repo boundary this feature deliberately does not
# cross (DESIGN-dispatch.md section 2). PLAN-repo-seams.md section 2a replaces this with a
# `capacity_wh` field on the plan itself; until that lands, the number is configured in both
# places and any change has to be made in both on the same day. 27,900 is the commandable
# capacity, not the pack's absolute ceiling -- see DESIGN-dispatch.md section 4.1.
DEFAULT_CAPACITY_WH = 27900.0


def atomic_write(path: Path, doc: dict) -> None:
    """Write `slots.json` so a reader never sees a half-written file. See the module docstring.

    The temp file is a sibling, not in /tmp: `Path.replace` is only atomic within a filesystem,
    and in the container /tmp and the slots volume are different mounts.

    A failed write takes the temp file with it. The volume is small and long-lived, and the
    debris would otherwise sit next to `slots.json` looking like a real artefact -- a truncated
    plan is exactly the thing somebody debugging a quiet battery does not need to find.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    try:
        tmp.write_text(json.dumps(doc, indent=1) + "\n")
        tmp.replace(path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def upcoming(intervals: list, now: dt.datetime) -> list:
    """Drop intervals that have already finished.

    The query reaches an hour back so the CURRENT interval is never missed; without this trim
    that lookback would also land three finished intervals in the file, where they do nothing
    but make `horizon_end` and the slot list disagree with what the dispatcher can act on.
    """
    step = dt.timedelta(minutes=interval_minutes(sorted(intervals, key=lambda i: i.start)))
    return [iv for iv in intervals if iv.start + step > now]


def translate(query_api, bucket: str, now: dt.datetime, capacity_wh: float) -> tuple[dict, list]:
    """Plan in InfluxDB -> the slots document. Raises `PlanFormatError` on anything unusable.

    Split out from `run()` so the whole read-and-translate path can be tested with a fake
    query API and no filesystem.
    """
    raw = newest_by_interval(from_influx(query_api, bucket, now - LOOKBACK, now + LOOKAHEAD))
    # Thinness is decided BEFORE the cadence is inferred. `upcoming()` has to call
    # `interval_minutes()`, which needs two intervals to see a gap at all -- so a window
    # holding a single trailing interval used to fail as "need at least two intervals to infer
    # the cadence", reporting a malformed plan for what is really a planner that stopped.
    # Monitor #2's whole job is telling those two apart. (Zero intervals never reaches here:
    # `from_influx` reports the empty window itself, naming the bucket and the range.)
    if len(raw) < 2:
        raise PlanFormatError(
            f"a lone plan interval at {raw[0].start.isoformat()} in the window ending "
            f"{(now + LOOKAHEAD).isoformat()} -- the planner has not run since")
    intervals = upcoming(raw, now)
    # And again after the trim, for the same reason: a plan on its last interval is a plan
    # about to lapse, not a plan with a broken cadence. The two guards look redundant and are
    # not -- this one fires on a plan that WAS translatable an hour ago and has since drained
    # past `now`, which is what a stalled planner actually looks like from here.
    if len(intervals) < 2:
        raise PlanFormatError(
            f"the newest plan is down to {len(intervals)} interval(s) after "
            f"{now.isoformat()} -- the planner has not run since")
    return build_document(intervals, capacity_wh, now)


def summarise(doc: dict, warnings: list) -> str:
    """One line, for the log and for the Kuma message.

    Kuma renders this into the notification, which is read on a phone, so it leads with the
    two facts that decide whether to get up: how far ahead the battery is committed, and which
    plan run it is following.
    """
    counts: dict[str, int] = {}
    for slot in doc["slots"]:
        counts[slot["action"]] = counts.get(slot["action"], 0) + 1
    mix = " ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    tail = f", {len(warnings)} warning(s)" if warnings else ""
    return f"{len(doc['slots'])} slots to {doc['horizon_end']} ({mix}) from {doc['plan_run']}{tail}"


def run(query_api, bucket: str, slots_path: Path, capacity_wh: float, now: dt.datetime,
        plan_url: str = "", slots_url: str = "", dry_run: bool = False) -> int:
    """One translation, with both heartbeats. Returns a process exit code.

    The two monitors are pinged at different points on purpose, and only one of them is ever
    the first to go down: a read failure reports #2 and leaves #3 silent, because "the plan
    could not be read" is not evidence about the translator. #3 only speaks once #2 is up.
    """
    try:
        doc, warnings = translate(query_api, bucket, now, capacity_wh)
    except PlanFormatError as e:
        # The field name (or the empty window) travels in the message -- PLAN-repo-seams.md
        # section 2b. An alert reading "no ping received" would not distinguish a renamed
        # field from a dead NAS.
        log.error("plan unreadable: %s", e)
        send_heartbeat(plan_url, "down", str(e)[:200])
        return 1
    except Exception as e:
        # Deliberately broad. `from_influx` speaks `PlanFormatError`, but the client under it
        # speaks HTTP: an expired token, a DNS failure or a NAS reboot arrives as an
        # `ApiException` or a socket error, and letting those escape would exit the run having
        # pinged NOTHING. Kuma would eventually notice the silence, but "no ping received" is
        # the one message that cannot say whether the planner, the database or the credentials
        # broke -- which is the whole reason this module pings at all. The type name goes in
        # the message because it is often the only part of an HTTP failure worth reading.
        log.exception("plan read failed")
        send_heartbeat(plan_url, "down", f"{type(e).__name__}: {e}"[:200])
        return 1
    send_heartbeat(plan_url, "up", f"plan_run {doc['plan_run']}")

    line = summarise(doc, warnings)
    for w in warnings:
        log.warning("translator: %s", w)

    if dry_run:
        log.info("[dry-run] would write %s: %s", slots_path, line)
        return 0

    try:
        atomic_write(slots_path, doc)
    except OSError as e:
        log.error("could not write %s: %s", slots_path, e)
        send_heartbeat(slots_url, "down", f"slots write failed: {e}"[:200])
        return 1

    log.info("wrote %s: %s", slots_path, line)
    send_heartbeat(slots_url, "up", line[:200])
    return 0


def build_query_api(url: str, token: str, org: str):
    """The InfluxDB read side, or None when it is not configured.

    Unlike `scheduler.build_publisher`, a missing token here is fatal rather than degraded:
    the publisher is observability, this is the only input the control path has.
    """
    if not (url and token):
        raise SystemExit(
            "INFLUX_URL and INFLUX_TOKEN must be set -- the token needs read on the "
            "`planning` bucket (DEPLOY.md, \"Scoped tokens\")")
    from influxdb_client import InfluxDBClient

    return InfluxDBClient(url=url, token=token, org=org).query_api()


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--slots", default="slots.json")
    p.add_argument("--bucket", default=os.environ.get("PLANNING_BUCKET", "planning"))
    p.add_argument("--influx-url", default=os.environ.get("INFLUX_URL", ""))
    p.add_argument("--influx-org", default=os.environ.get("INFLUX_ORG", "home"))
    p.add_argument("--capacity-wh", type=float,
                   default=float(os.environ.get("BATTERY_CAPACITY_WH") or DEFAULT_CAPACITY_WH))
    p.add_argument("--dry-run", action="store_true", help="Translate and report, write nothing")
    p.add_argument("-v", "--verbose", action="store_true")
    a = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s")

    # The token is read from the environment only -- never a flag, so it cannot end up in a
    # shell history or a `ps` listing on a shared NAS.
    query_api = build_query_api(a.influx_url, os.environ.get("INFLUX_TOKEN", ""), a.influx_org)
    sys.exit(run(
        query_api, a.bucket, Path(a.slots), a.capacity_wh, dt.datetime.now(dt.UTC),
        plan_url=os.environ.get("PLAN_INFLUX_HEARTBEAT_URL", ""),
        slots_url=os.environ.get("SLOTS_WRITTEN_HEARTBEAT_URL", ""),
        dry_run=a.dry_run))


if __name__ == "__main__":
    main()
