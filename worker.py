#!/usr/bin/env python3
"""Run the odds monitor continuously without starting an HTTP server."""

from __future__ import annotations

import logging
import time

import requests

from main import ConfigurationError, Config, OddsMonitor


LOGGER = logging.getLogger("odds-monitor-worker")


def run_forever() -> None:
    try:
        config = Config.from_environment()
    except ConfigurationError as exc:
        LOGGER.error("%s", exc)
        raise SystemExit(2) from exc

    monitor = OddsMonitor(config)
    LOGGER.info("Background odds worker started; polling every %d seconds", config.poll_interval)

    while True:
        poll_started = time.monotonic()
        try:
            monitor.run_once()
        except requests.RequestException as exc:
            LOGGER.error("Odds API request failed: %s", exc)
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
    logging.basicConfig(
        level=getattr(logging, "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    run_forever()