"""
Diagnostic — verify Kalshi API auth with a READ-ONLY GET /portfolio/balance (no money).
Reads creds from .env, RSA-PSS signs against prod and demo, and reports which base
returns 200 (= the environment your key belongs to). Prints status codes only.
Run:  .venv/bin/python -m tools.verify_kalshi_auth
"""

import base64
import os
import sys
import time

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

BASES = {"prod": "https://external-api.kalshi.com/trade-api/v2",
         "demo": "https://external-api.demo.kalshi.co/trade-api/v2"}
PATH = "/portfolio/balance"


def load_env(path: str = ".env") -> dict:
    env = {}
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.split(" #", 1)[0].strip()
    return env


def load_key(v: str):
    if v.startswith("-----BEGIN"):
        return serialization.load_pem_private_key(v.replace("\\n", "\n").encode(), password=None)
    with open(os.path.expanduser(v), "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def main() -> int:
    e = load_env()
    kid, pk = e.get("KALSHI_API_KEY_ID", ""), e.get("KALSHI_PRIVATE_KEY_PATH", "")
    if not (kid and pk):
        print("Set KALSHI_API_KEY_ID + KALSHI_PRIVATE_KEY_PATH in .env first.")
        return 1
    try:
        key = load_key(pk)
    except Exception as ex:
        # type name only: the exception text can carry the key path value
        print(f"Could not load the RSA key: {type(ex).__name__}")
        print("KALSHI_PRIVATE_KEY_PATH must point to your full multi-line .pem file.")
        return 1
    ok = False   # exit 0 only if at least one base answered 200
    for name, base in BASES.items():
        full = "/trade-api/v2" + PATH
        ts = str(int(time.time() * 1000))
        sig = base64.b64encode(key.sign((ts + "GET" + full).encode(),
              padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
              hashes.SHA256())).decode()
        try:
            r = httpx.get(base + PATH, timeout=10.0, headers={
                "KALSHI-ACCESS-KEY": kid, "KALSHI-ACCESS-SIGNATURE": sig, "KALSHI-ACCESS-TIMESTAMP": ts})
            ok = ok or r.status_code == 200
            tag = "  <== YOUR BASE (set KALSHI_API_BASE to this)" if r.status_code == 200 else ""
            print(f"  {name:4} {base} -> {r.status_code}{tag}")
        except Exception as ex:
            print(f"  {name:4} {base} -> ERR {type(ex).__name__}")
    print("\n200 = key + signing + base all good. Paste the result back (status codes only).")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
