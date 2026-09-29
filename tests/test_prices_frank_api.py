"""Frank's post-2026-09 API: cumulative prices -> the four stored components.

No network: `requests.post` is stubbed with a canned response taken from the live API.
"""
import datetime as dt

import prices


class _Resp:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def _row(market, plus, all_in, frm="2026-09-28T22:00:00.000Z", till="2026-09-28T22:15:00.000Z"):
    return {"from": frm, "till": till, "marketPrice": market, "marketPricePlus": plus,
            "allInPrice": all_in, "perUnit": "KWH"}


def _fetch(monkeypatch, body, day=dt.date(2026, 9, 29)):
    sent = {}

    def post(url, json, **kw):
        sent.update(url=url, json=json)
        return _Resp(body)

    monkeypatch.setattr(prices.requests, "post", post)
    return prices.fetch_prices_for_day(day), sent


def test_components_are_derived_and_resum_to_all_in(monkeypatch):
    body = {"data": {"marketPrices": {"electricityPrices": [_row(0.1953, 0.25446, 0.36531)]}}}
    (row,), _ = _fetch(monkeypatch, body)
    assert row["market_price"] == 0.1953
    assert abs(row["market_price_tax"] - 0.1953 * 0.21) < 1e-6
    assert abs(row["sourcing_markup"] - 0.01815) < 2e-5
    assert abs(row["energy_tax"] - 0.11085) < 1e-6
    assert abs(row["total"] - 0.36531) < 1e-6
    assert row["duration_s"] == 900.0


def test_resolution_follows_the_cutover(monkeypatch):
    empty = {"data": {"marketPrices": {"electricityPrices": []}}}
    _, sent = _fetch(monkeypatch, empty, day=dt.date(2026, 7, 31))
    assert sent["json"]["variables"] == {"date": "2026-07-31", "resolution": "PT60M"}
    _, sent = _fetch(monkeypatch, empty, day=dt.date(2026, 8, 1))
    assert sent["json"]["variables"] == {"date": "2026-08-01", "resolution": "PT15M"}


def test_unpublished_day_is_empty_not_an_error(monkeypatch):
    body = {"data": None, "errors": [{"message": "No marketprices found for segment ELECTRICITY for 2026-09-30"}]}
    rows, _ = _fetch(monkeypatch, body)
    assert rows == []


def test_validation_error_still_raises(monkeypatch):
    import pytest
    with pytest.raises(RuntimeError):
        _fetch(monkeypatch, {"data": None, "errors": [{"message": "Graphql validation error"}]})


def test_row_missing_a_price_is_skipped(monkeypatch):
    bad = _row(0.1, 0.2, 0.3)
    del bad["allInPrice"]
    rows, _ = _fetch(monkeypatch, {"data": {"marketPrices": {"electricityPrices": [bad]}}})
    assert rows == []


def test_run_counts_failed_days_but_not_unpublished_ones(monkeypatch):
    def fake_fetch(day):
        if day == dt.date(2026, 9, 28):
            raise RuntimeError("Graphql validation error")
        return []  # unpublished

    monkeypatch.setattr(prices, "fetch_prices_for_day", fake_fetch)
    days = [dt.date(2026, 9, 28), dt.date(2026, 9, 29), dt.date(2026, 9, 30)]
    assert prices.run(days, dry_run=True) == 1
