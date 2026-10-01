"""examples/fetch_market.py: offline, it converts a market page, prints a dry-run order body,
and never sends a request through the broker."""

import importlib.util
import json
from pathlib import Path

import pytest

import execute.kalshi_broker as kb
import fetch.kalshi as kalshi

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "tests" / "fixtures" / "kalshi_markets_list" / "KXMLBGAME_real_2026-09-16.json"


@pytest.fixture
def example(monkeypatch):
    for name in ("KALSHI_API_KEY_ID", "KALSHI_PRIVATE_KEY_PATH", "KALSHI_API_BASE"):
        monkeypatch.delenv(name, raising=False)
    sides = dict(kalshi.kalshi_sides)
    spec = importlib.util.spec_from_file_location("fetch_market_example",
                                                  ROOT / "examples" / "fetch_market.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    yield mod
    kalshi.kalshi_sides.clear()
    kalshi.kalshi_sides.update(sides)


def test_example_prints_a_dry_run_body_and_sends_nothing(example, monkeypatch, capsys):
    page = json.loads(PAGE.read_text())["markets"]
    monkeypatch.setattr(example, "fetch_markets", lambda series, client: page)

    def _no_send(*args, **kwargs):
        raise AssertionError("the example must never send a broker request")
    monkeypatch.setattr(kb.KalshiBroker, "_request", _no_send)
    monkeypatch.setattr(kb.KalshiBroker, "place_order", _no_send)

    assert example.main(["fetch_market.py", "KXMLBGAME"]) == 0
    out = capsys.readouterr().out
    assert "Kalshi credentials present: False" in out
    assert "NOT sent" in out
    body = json.loads(out.split("(NOT sent):", 1)[1])
    assert body["side"] == "bid"                       # buying the YES team is a 'bid'
    assert body["count"] == "1"
    assert body["client_order_id"] == "example-dry-run"
    assert body["ticker"] in kalshi.kalshi_sides


def test_example_rejects_a_non_winner_series(example, capsys):
    assert example.main(["fetch_market.py", "KXMLBTOTAL"]) == 2
    assert "non-winner series" in capsys.readouterr().out
