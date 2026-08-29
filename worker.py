#!/usr/bin/env python3
"""Run the odds monitor continuously without starting an HTTP server."""

from __future__ import annotations

import logging
import os
import time

import requests

from main import ConfigurationError, Config, OddsMonitor, _request_error_summary


LOGGER = logging.getLogger("odds-monitor-worker")


def run_forever() -> None:
    try:
        config = Config.from_environment()
    except ConfigurationError as exc:
        LOGGER.error("%s", exc)
        raise SystemExit(2) from exc

    monitor = OddsMonitor(config)
    LOGGER.info(
        "Background odds worker started; bet365 pre-match soccer polling every %d seconds",
        config.poll_interval,
    )
    LOGGER.info("The first successful poll establishes a silent baseline")

    while True:
        poll_started = time.monotonic()
        try:
            monitor.run_once()
        except requests.RequestException as exc:
            LOGGER.error("Odds API request failed: %s", _request_error_summary(exc))
        except (ValueError, RuntimeError) as exc:
            LOGGER.error("Odds poll failed: %s", exc)

        elapsed = time.monotonic() - poll_started
        sleep_for = max(0, config.poll_interval - elapsed)
        try:
            time.sleep(sleep_for)
        except KeyboardInterrupt:
            LOGGER.info("Stopping background odds worker")
            return


if __name__ == "__main__":
    log_level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    run_forever()