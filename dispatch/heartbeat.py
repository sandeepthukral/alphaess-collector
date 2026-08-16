"""Kuma push heartbeats. DESIGN-dispatch.md section 6.1.

One function, in its own module for one reason: both halves of this container need it, and
they live in different layers. `translate.py` pings monitors #2 and #3 around a batch job;
`scheduler.py` pings #4-#8 from inside the control loop. A copy in each would be two copies of
the query-string bug documented below, and that bug is silent -- every ping registers as DOWN
while the code looks correct.

Written against `urllib` rather than `requests` on purpose: this image ships pymodbus and
influxdb-client and nothing else, and a monitoring convenience is a poor reason to add a
dependency to a process that drives hardware.
"""
from __future__ import annotations

import logging
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import urlopen

log = logging.getLogger("dispatch")


def send_heartbeat(url: str, status: str = "up", msg: str = "OK", timeout: float = 5) -> None:
    """Ping a Kuma 'Push' monitor. Never raises, and does nothing when `url` is empty.

    The same shape as `collector/collector.py:407`, including the rebuilt query string -- the
    push URL Kuma displays already carries `?status=up&msg=OK&ping=`, and appending to it
    makes Express parse `status` as an array, which matches neither value, so every ping
    registers as DOWN. Rebuild the query rather than adding to it.

    An unset URL is the documented "not monitored yet" state, not an error: monitors are
    created during go-live (DISPATCH-GOLIVE.md section 3) and the loop has to run before that.
    """
    if not url:
        return
    target = urlunsplit(urlsplit(url)._replace(query=urlencode({"status": status, "msg": msg})))
    try:
        with urlopen(target, timeout=timeout):
            pass
    except Exception as exc:
        log.warning("heartbeat ping failed: %s", exc)
