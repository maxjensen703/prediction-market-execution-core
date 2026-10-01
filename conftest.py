"""Pytest-wide setup. Keeps the suite away from real credentials and real on-disk event logs."""

import os
import tempfile

import pytest

# config.py loads ./.env from the current working directory at import. Run the suite from an
# empty temporary directory so no developer .env (and no real credential) is ever loaded into a
# test process. Every test resolves its files relative to __file__, never the working directory.
os.chdir(tempfile.mkdtemp(prefix="pmec-tests-"))


@pytest.fixture(autouse=True)
def _isolate_private_ws_events(tmp_path, monkeypatch):
    """Redirect the private-WS raw order-event log into a temp dir so the suite never writes
    data/maker_events.jsonl (the only on-disk write the execution layer makes in tests)."""
    import stream.polymarket_us_private_ws as pw
    monkeypatch.setattr(pw, "MAKER_EVENTS_FILE", str(tmp_path / "events.jsonl"))
