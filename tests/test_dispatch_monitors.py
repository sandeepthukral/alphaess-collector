"""The five Kuma monitors the control loop owns. DESIGN-dispatch.md section 6.1, #4-#8.

`scheduler.monitor_pings()` is pure, so the whole monitoring contract is testable without a
bus, a network or a clock -- which matters more here than usual, because the failure this
guards against is not a wrong ping but a MISSING one. A monitor that is documented and never
pinged looks identical to a healthy system right up until the moment it was supposed to fire.
Section 6.1's table is transcribed into `MONITORS` below and asserted against, so adding a row
there without wiring it fails here.

The scheduler module imports cleanly without pymodbus (the client import is guarded), so these
run in CI whether or not the image is available.
"""
from __future__ import annotations

import heartbeat as H
import scheduler
import slots as S

# Section 6.1, the rows this process is responsible for.
MONITORS = {"slots-fresh", "dispatcher-alive", "dispatch-confirmed",
            "inverter-not-hijacked", "soc-floor"}


def pings(decision, cache=None, live_soc=50.0, dry_run=False):
    return dict((name, (status, msg)) for name, status, msg in
                scheduler.monitor_pings(decision, cache or {}, live_soc, dry_run))


def commanded(reason="charge 4848 W to 26.1%"):
    return S.Decision("command", reason, slot={"action": "charge"})


class TestSendHeartbeat:
    """The ping itself. Shared by the translator (#2, #3) and the loop (#4-#8), which is why
    it is one module and one set of tests rather than a copy on each side."""

    def test_no_url_is_not_an_error(self):
        H.send_heartbeat("", "up", "OK")

    def test_the_query_string_is_rebuilt_not_appended(self, monkeypatch):
        """Kuma's own push URL already carries ?status=up&msg=OK&ping=. Appending makes
        Express parse status as an array, which matches neither value, so every ping registers
        as DOWN -- the bug collector.py's send_heartbeat exists to avoid."""
        seen = []

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(H, "urlopen", lambda url, timeout=5: seen.append(url) or Resp())
        H.send_heartbeat("http://kuma/api/push/abc?status=up&msg=OK&ping=", "down", "why")
        assert seen == ["http://kuma/api/push/abc?status=down&msg=why"]

    def test_a_failed_ping_never_raises(self, monkeypatch):
        """A Kuma outage must never reach the control loop."""
        def boom(url, timeout=5):
            raise OSError("no route to host")

        monkeypatch.setattr(H, "urlopen", boom)
        H.send_heartbeat("http://kuma/api/push/abc", "up", "OK")


class TestEveryDocumentedMonitorIsWired:
    def test_the_url_table_covers_exactly_the_dispatcher_s_monitors(self):
        """The gap this file exists for: five monitors in the design, none in the code."""
        assert set(scheduler.MONITOR_URLS) == MONITORS

    def test_a_healthy_tick_pings_all_five(self):
        sent = pings(commanded(), {"write_verified": True})
        assert set(sent) == MONITORS
        assert all(status == "up" for status, _ in sent.values())

    def test_an_unset_url_makes_the_ping_a_no_op_rather_than_an_error(self, monkeypatch):
        """Monitors are created during go-live; the loop has to run before that."""
        monkeypatch.setitem(scheduler.MONITOR_URLS, "soc-floor", "")
        scheduler.send_heartbeat("", "up", "OK")  # must not raise, must not reach the network


class TestSlotsFresh:
    def test_a_stale_plan_takes_monitor_4_down_with_the_reason(self):
        stale = S.Decision("idle", "plan is 9.2 h old (limit 4 h)", fresh=False)
        assert pings(stale)["slots-fresh"] == ("down", "plan is 9.2 h old (limit 4 h)")

    def test_a_gap_between_slots_is_not_staleness(self):
        """#4 is "the translator died", not "there is nothing to do right now". A plan with a
        legitimate gap must not page anybody."""
        gap = S.Decision("idle", "no slot covers 2026-08-16T11:00:00+00:00")
        assert pings(gap)["slots-fresh"][0] == "up"


class TestDispatchConfirmed:
    def test_a_write_that_did_not_verify_takes_monitor_6_down(self):
        status, msg = pings(commanded(), {"write_verified": False})["dispatch-confirmed"]
        assert status == "down"
        assert "did not verify" in msg

    def test_nothing_to_confirm_is_up_not_down(self):
        """A release writes no command, so a readback proving nothing is not evidence of a
        rejected write. Reporting down here would page for a working self-consumption hour."""
        released = S.Decision("release", "plan wants self-consumption")
        status, msg = pings(released, {"write_verified": None})["dispatch-confirmed"]
        assert status == "up"
        assert "nothing commanded" in msg

    def test_dry_run_never_reports_a_failed_write(self):
        """Dry run writes nothing, so every readback mismatches. Left unguarded this monitor
        would sit red for the whole observation phase and be ignored by the time it mattered."""
        status, msg = pings(commanded(), {"write_verified": False}, dry_run=True)[
            "dispatch-confirmed"]
        assert status == "up"
        assert "dry run" in msg


class TestHijack:
    def test_the_app_driving_the_block_takes_monitor_7_down(self):
        """The 2026-08-15 signature, and the reason this monitor is not theoretical."""
        cache = {"hijacked": True,
                 "hijack_state": {"mode": 2, "power_w": 5000, "target_soc_pct": 100.0}}
        status, msg = pings(commanded(), cache)["inverter-not-hijacked"]
        assert status == "down"
        assert "mode=2" in msg and "5000" in msg and "100.0" in msg

    def test_an_unhijacked_block_is_up(self):
        assert pings(commanded(), {"hijacked": False})["inverter-not-hijacked"][0] == "up"


class TestSocFloor:
    def test_below_the_floor_takes_monitor_8_down(self):
        status, msg = pings(commanded(), live_soc=S.SOC_FLOOR_PCT - 0.5)["soc-floor"]
        assert status == "down"
        assert f"floor {S.SOC_FLOOR_PCT}" in msg

    def test_exactly_at_the_floor_is_not_a_breach(self):
        assert pings(commanded(), live_soc=S.SOC_FLOOR_PCT)["soc-floor"][0] == "up"

    def test_an_unreadable_soc_is_silence_not_a_breach(self):
        """A `down` would claim the battery is below its floor, which was not observed. The
        15-minute window still turns a PERSISTENT read failure into a down on its own."""
        assert "soc-floor" not in pings(commanded(), live_soc=None)

    def test_every_message_fits_a_kuma_notification(self):
        long_reason = S.Decision("idle", "x" * 500, fresh=False)
        assert all(len(msg) <= 200 for _s, msg in pings(long_reason).values())
