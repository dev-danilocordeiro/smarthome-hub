"""Write the API's OpenAPI document to packages/contracts/openapi.json.

The web app's TypeScript types are generated from it (`make gen-client`). CI runs this
with `--check` and fails if the committed document is stale.

    apps/api/.venv/bin/python scripts/export_openapi.py [--check]
"""

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "packages" / "contracts" / "openapi.json"


def render() -> str:
    # No collector, no JSON logs: only the route table matters here.
    os.environ.setdefault("SMARTHOME_OTEL_ENABLED", "false")
    os.environ.setdefault("SMARTHOME_LOG_JSON", "false")
    from smarthome.main import create_app  # noqa: PLC0415 - after the environment is set

    document = create_app().openapi()
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the file is stale")
    args = parser.parse_args()
    rendered = render()
    if args.check:
        current = OUTPUT.read_text() if OUTPUT.exists() else ""
        if current != rendered:
            print(f"{OUTPUT.relative_to(ROOT)} is stale; run `make gen-client`", file=sys.stderr)
            return 1
        return 0
    OUTPUT.write_text(rendered)
    print(f"wrote {OUTPUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
