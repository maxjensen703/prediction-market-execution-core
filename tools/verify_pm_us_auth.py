"""
Diagnostic — Polymarket US. Signing is CONFIRMED (timestamp+method+path+body). This
sweeps candidate READ paths (GET only — no orders, no money) with a valid signature
to find which endpoints exist: 200/405 = real endpoint, 404 = not a path, 401 = auth
problem. Run:  .venv/bin/python -m tools.verify_pm_us_auth
"""

import base64
import sys
import time

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

BASE_DEFAULT = "https://api.polymarket.us"
CANDIDATES = [
    "/v1/balances", "/v1/balance", "/v1/account", "/v1/accounts", "/v1/positions",
    "/v1/portfolio", "/v1/orders", "/v1/order", "/v1/open-orders", "/v1/openorders",
    "/v1/fills", "/v1/trades", "/v1/user", "/v1/users/me", "/v1/me", "/v1/wallet",
    "/v1/funds", "/v1/account/balances", "/balances", "/account",
]


def load_env(path: str = ".env") -> dict:
    env = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def main() -> int:
    env = load_env()
    key_id = env.get("POLYMARKET_US_API_KEY", "")
    secret = env.get("POLYMARKET_US_PRIVATE_KEY", "")
    base = env.get("POLYMARKET_US_API_BASE", "") or BASE_DEFAULT
    if not (key_id and secret):
        print("Set POLYMARKET_US_API_KEY + POLYMARKET_US_PRIVATE_KEY in .env first.")
        return 1
    sk = Ed25519PrivateKey.from_private_bytes(base64.b64decode(secret)[:32])

    def headers(method: str, path: str, body: str = "") -> dict:
        ts = str(int(time.time() * 1000))
        sig = base64.b64encode(sk.sign((ts + method + path + body).encode())).decode()
        return {"X-PM-Access-Key": key_id, "X-PM-Timestamp": ts, "X-PM-Signature": sig}

    print(f"base={base}  signing=ts+method+path+body (confirmed)\n")
    with httpx.Client(timeout=10.0) as c:
        for p in CANDIDATES:
            try:
                r = c.get(base + p, headers=headers("GET", p))
                flag = "  <== EXISTS" if r.status_code in (200, 405) else ""
                print(f"  GET {p:24} -> {r.status_code}{flag}")
            except Exception as e:
                print(f"  GET {p:24} -> ERR {type(e).__name__}")
    print("\nPaste the lines marked EXISTS (status only). 405 on an orders path = that path is the POST order endpoint.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
