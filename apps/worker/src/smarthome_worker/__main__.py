"""Composition root for the `worker` process.

Same modules as the API, different entrypoint (ADR 0001). The real loop (outbox relay and schedules)
arrives in phase 8; for now it proves the process boots with shared config and logging.
"""

import structlog

from smarthome.shared.config import get_settings
from smarthome.shared.logging import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json=settings.log_json)
    structlog.get_logger(__name__).info(
        "entrypoint_booted", entrypoint="worker", implemented_in_phase=8
    )


if __name__ == "__main__":
    main()
