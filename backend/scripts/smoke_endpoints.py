"""Hit every list endpoint and assert it returns 200 with a JSON list.

WHY THIS EXISTS. On 2026-08-10 a one-line change made /tennis/markets raise
AttributeError on every request -- it accessed `.ts` on Row objects that
deliberately carry only three columns. The endpoint returned HTTP 500 for hours,
through a commit and a push, because nothing checks that the routes still
answer. It was found only when a later, unrelated verification happened to curl
it.

Nothing here asserts anything about MODEL quality -- that is what the backtests
and the integrity checks are for. This answers one question the app had no
answer to: does every route still respond at all?

Run against a live backend:
    python scripts/smoke_endpoints.py
    python scripts/smoke_endpoints.py --timeout 900   # cold caches after a restart

Exit code is the number of failing endpoints, so it is usable as a gate.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8756"

# Derived from the sport registry so a newly added sport is covered without
# anyone remembering to edit this list -- the same drift that has bitten the
# cache warmer, the paper logger and the catalog scanner.
try:
    sys.path.insert(0, ".")
    from app import sports as app_sports

    SPORT_PATHS = list(app_sports.MARKETS_PATHS) + list(app_sports.FUTURES_PATHS)
except Exception:  # pragma: no cover - script must run even outside the package
    SPORT_PATHS = []

OTHER_PATHS = [
    "/health",
    "/warmup",
    "/settings",
    "/markets/readiness",
    "/markets/cross-platform-divergences",
    "/placed-bets/clv-buckets",
    "/catalog/new",
    "/catalog/flagged",
]


def check(path: str, timeout: float) -> tuple[bool, str]:
    started = time.time()
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            body = r.read()
            took = time.time() - started
            if r.status != 200:
                return False, f"HTTP {r.status}"
            try:
                data = json.loads(body.decode())
            except Exception as exc:
                return False, f"non-JSON body ({type(exc).__name__})"
            n = len(data) if isinstance(data, (list, dict)) else "?"
            return True, f"200 in {took:5.1f}s, {n} items"
    except urllib.error.HTTPError as exc:
        # THE CASE THIS SCRIPT EXISTS FOR. Print the body: FastAPI puts the
        # exception type there, which is the difference between "the route is
        # broken" and "the route is fine but slow".
        detail = ""
        try:
            detail = exc.read().decode()[:200]
        except Exception:
            pass
        return False, f"HTTP {exc.code} {detail}"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {str(exc)[:120]}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=600.0)
    args = ap.parse_args()

    paths = OTHER_PATHS + SPORT_PATHS
    failures = []
    for p in paths:
        ok, msg = check(p, args.timeout)
        print(f"{'ok  ' if ok else 'FAIL'}  {p:38s} {msg}")
        if not ok:
            failures.append((p, msg))

    print()
    print(f"{len(paths) - len(failures)}/{len(paths)} endpoints healthy")
    if failures:
        print("\nFAILING:")
        for p, msg in failures:
            print(f"   {p}: {msg}")
    return len(failures)


if __name__ == "__main__":
    raise SystemExit(main())
