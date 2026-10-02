"""Send a realistic mix of requests to /diagnostics/trace-demo so dashboards have data.

Stdlib only, so it runs with any Python 3.12+ without installing anything.

    python3 scripts/demo_traffic.py [--seconds 120] [--rps 10] [--base-url http://localhost:8000]
"""

import argparse
import random
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

# (weight, query string): mostly fast, a tail of slow requests and a few failures.
MIX = [
    (80, ""),
    (12, "?delay_ms=150"),
    (5, "?delay_ms=900"),
    (3, "?fail=true"),
]


def hit(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310 - fixed local URL
            return int(response.status)
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=120)
    parser.add_argument("--rps", type=float, default=10)
    parser.add_argument("--base-url", default="http://localhost:8000")
    args = parser.parse_args()

    endpoint = f"{args.base_url}/diagnostics/trace-demo"
    weights = [w for w, _ in MIX]
    queries = [q for _, q in MIX]
    counts: dict[int, int] = {}
    deadline = time.monotonic() + args.seconds

    with ThreadPoolExecutor(max_workers=32) as pool:
        futures = []
        while time.monotonic() < deadline:
            query = random.choices(queries, weights)[0]  # noqa: S311 - not security sensitive
            futures.append(pool.submit(hit, endpoint + query))
            time.sleep(1 / args.rps)
        for future in futures:
            status = future.result()
            counts[status] = counts.get(status, 0) + 1

    print("status counts:", dict(sorted(counts.items())))
    return 1 if counts.get(0) else 0


if __name__ == "__main__":
    sys.exit(main())
