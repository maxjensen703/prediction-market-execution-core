"""tools/verify_kalshi_auth.py exits 0 if and only if at least one of its two
hard-coded bases answers 200, prints the same lines as before, and never prints an env value.
The HTTP layer, the env reader and the key loader are stubbed: no network, no .env, no key."""

from __future__ import annotations

import pytest

import tools.verify_kalshi_auth as auth_tool


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class _Key:
    def sign(self, *args, **kwargs):
        return b"sig"


def _run_auth(monkeypatch, capsys, answers):
    monkeypatch.setattr(auth_tool, "load_env", lambda path=".env": {
        "KALSHI_API_KEY_ID": "KID-SENTINEL", "KALSHI_PRIVATE_KEY_PATH": "PK-SENTINEL"})
    monkeypatch.setattr(auth_tool, "load_key", lambda v: _Key())
    by_base = dict(zip(auth_tool.BASES.values(), answers))

    def fake_get(url, **kwargs):
        answer = by_base[url[: -len(auth_tool.PATH)]]
        if isinstance(answer, Exception):
            raise answer
        return _Resp(answer)
    monkeypatch.setattr(auth_tool.httpx, "get", fake_get)
    code = auth_tool.main()
    out = capsys.readouterr().out
    assert "KID-SENTINEL" not in out and "PK-SENTINEL" not in out
    return code, out


@pytest.mark.parametrize("answers,expected", [
    ((200, 401), 0), ((401, 200), 0), ((200, 200), 0),
    ((401, 401), 1), ((500, 403), 1),
    ((ConnectionError("x"), ConnectionError("y")), 1), ((ConnectionError("x"), 200), 0),
])
def test_auth_tool_exits_zero_only_when_a_base_answers_200(monkeypatch, capsys, answers, expected):
    code, _ = _run_auth(monkeypatch, capsys, answers)
    assert code == expected


@pytest.mark.parametrize("make_path", [
    lambda d: d / "PK-SENTINEL-missing.pem",                  # FileNotFoundError names the path
    lambda d: d,                                               # a directory: IsADirectoryError
])
def test_a_bad_key_path_is_reported_by_type_only_and_never_echoed(monkeypatch, capsys, tmp_path,
                                                                 make_path):
    key_dir = tmp_path / "PK-SENTINEL-dir"
    key_dir.mkdir()
    key_path = str(make_path(key_dir))
    monkeypatch.setattr(auth_tool, "load_env", lambda path=".env": {
        "KALSHI_API_KEY_ID": "KID-SENTINEL", "KALSHI_PRIVATE_KEY_PATH": key_path})

    def no_network(*args, **kwargs):
        raise AssertionError("the tool reached HTTP with an unloadable key")
    monkeypatch.setattr(auth_tool.httpx, "get", no_network)
    assert auth_tool.main() == 1          # the real load_key raises; no base answered 200
    out = capsys.readouterr().out
    assert "Could not load the RSA key:" in out
    assert "PK-SENTINEL" not in out and "KID-SENTINEL" not in out and key_path not in out


def test_auth_tool_printed_lines_are_unchanged(monkeypatch, capsys):
    _, out = _run_auth(monkeypatch, capsys, (401, 200))
    prod, demo = auth_tool.BASES["prod"], auth_tool.BASES["demo"]
    assert out == (f"  prod {prod} -> 401\n"
                   f"  demo {demo} -> 200  <== YOUR BASE (set KALSHI_API_BASE to this)\n"
                   "\n200 = key + signing + base all good. Paste the result back "
                   "(status codes only).\n")
