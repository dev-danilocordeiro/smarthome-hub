"""Static checks for provisioned Grafana dashboards.

Catches the mistakes Grafana only reports at runtime (or silently ignores):
duplicate dashboard uids, duplicate panel ids, and panels pointing at a datasource
uid that is not provisioned.

    python3 scripts/check_dashboards.py
"""

import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DASHBOARDS = ROOT / "infra" / "grafana" / "dashboards"
DATASOURCES = ROOT / "infra" / "grafana" / "provisioning" / "datasources" / "datasources.yaml"


def provisioned_uids() -> set[str]:
    # Stdlib only: the provisioning file is flat enough for a regex.
    return set(re.findall(r"^\s+uid:\s*(\S+)\s*$", DATASOURCES.read_text(), re.MULTILINE))


def walk_panels(panels: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    for panel in panels:
        yield panel
        yield from walk_panels(panel.get("panels", []))


def datasource_uids(node: object) -> Iterator[str]:
    if isinstance(node, dict):
        ds = node.get("datasource")
        if isinstance(ds, dict) and isinstance(ds.get("uid"), str):
            yield ds["uid"]
        for value in node.values():
            yield from datasource_uids(value)
    elif isinstance(node, list):
        for item in node:
            yield from datasource_uids(item)


def main() -> int:
    known = provisioned_uids()
    errors: list[str] = []
    seen_uids: dict[str, Path] = {}

    for path in sorted(DASHBOARDS.glob("*.json")):
        name = path.relative_to(ROOT)
        try:
            dashboard = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            errors.append(f"{name}: invalid JSON ({exc})")
            continue

        uid = dashboard.get("uid")
        if not uid:
            errors.append(f"{name}: missing uid")
        elif uid in seen_uids:
            errors.append(f"{name}: uid {uid!r} already used by {seen_uids[uid]}")
        else:
            seen_uids[uid] = name

        ids = [p.get("id") for p in walk_panels(dashboard.get("panels", []))]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            errors.append(f"{name}: duplicate panel ids {sorted(duplicates)}")

        errors.extend(
            f"{name}: datasource uid {ds!r} is not provisioned"
            for ds in sorted(set(datasource_uids(dashboard)) - known)
        )

    for error in errors:
        print(error, file=sys.stderr)
    print(f"checked {len(seen_uids)} dashboard(s) against datasources {sorted(known)}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
