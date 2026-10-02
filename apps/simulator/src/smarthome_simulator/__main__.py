"""Device simulator entrypoint.

Depends only on `device-protocol`, exactly like a real ESP32 would. Behaviour arrives in phase 4.
"""

import json
import logging
import sys

from device_protocol import PROTOCOL_VERSION


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(message)s")
    logging.getLogger(__name__).info(
        json.dumps(
            {
                "event": "entrypoint_booted",
                "entrypoint": "simulator",
                "protocol_version": PROTOCOL_VERSION,
                "implemented_in_phase": 4,
            }
        )
    )


if __name__ == "__main__":
    main()
