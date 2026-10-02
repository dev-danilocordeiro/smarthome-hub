"""Composition root for the `ingestor` process.

Same modules as the API, different entrypoint (ADR 0001). The real loop (telemetry ingestion)
arrives in phase 6; for now it proves the process boots with shared config and logging.
"""

import structlog

from smarthome.shared.config import get_settings
from smarthome.shared.logging import configure_logging


def main() -> None:
    settings = get_settings()
    configure_logging(level=settings.log_level, json=settings.log_json)
    structlog.get_logger(__name__).info(
        "entrypoint_booted", entrypoint="ingestor", implemented_in_phase=6
    )


if __name__ == "__main__":
    main()
