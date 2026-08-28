#!/usr/bin/env python3
"""Monitor sports odds and send Telegram alerts.

The default adapter is compatible with The Odds API v4. Configure it with
environment variables, for example:

    export ODDS_API_KEY="your-odds-api-key"
    export TELEGRAM_BOT_TOKEN="your-bot-token"
    export TELEGRAM_CHAT_ID="your-chat-id"
    export ODDS_SPORT="soccer_epl"
    python main.py

The script stores the previous poll in ``ODDS_STATE_FILE`` (default:
``odds_state.json``). The first poll establishes a baseline and does not
generate drop alerts.

The Odds API normally returns prices but not market lock status. Lock alerts
are emitted when an odds provider includes an explicit status such as
``suspended``, ``locked``, or ``closed`` in a market/bookmaker/event object.
This keeps the lock detection useful with APIs that expose those fields while
avoiding false positives when a market is simply absent from a response.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from threading import Thread
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from flask import Flask, jsonify
import requests


LOGGER = logging.getLogger("odds-monitor")
DEFAULT_ODDS_URL = "https://api.the-odds-api.com/v4/sports/{sport}/odds/"
DEFAULT_TELEGRAM_URL = "https://api.telegram.org"
LOCKED_STATUSES = {
    "closed",
    "halted",
    "inactive",
    "locked",
    "offline",
    "suspended",
    "unavailable",
}
HEALTH_PORT = 8080
health_app = Flask("odds-monitor-health")


@health_app.get("/health")
def health() -> tuple[Any, int]:
    return jsonify({"status": "ok", "service": "odds-monitor"}), 200


def start_health_server() -> Thread:
    server_thread = Thread(
        target=lambda: health_app.run(
            host="0.0.0.0",
            port=HEALTH_PORT,
            threaded=True,
            use_reloader=False,
        ),
        name="health-server",
        daemon=True,
    )
    server_thread.start()
    return server_thread


class ConfigurationError(ValueError):
    """Raised when required environment configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    odds_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    odds_url: str
    sport: str
    regions: str
    markets: str
    odds_format: str
    poll_interval: int
    drop_threshold: float
    state_file: Path
    request_timeout: int

    @classmethod
    def from_environment(cls) -> "Config":
        required = ("ODDS_API_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID")
        missing = [name for name in required if not os.getenv(name)]
        if missing:
            raise ConfigurationError(
                "Missing required environment variable(s): " + ", ".join(missing)
            )

        try:
            poll_interval = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
            drop_threshold = float(os.getenv("DROP_THRESHOLD_PERCENT", "8"))
            request_timeout = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))
        except ValueError as exc:
            raise ConfigurationError(
                "POLL_INTERVAL_SECONDS, DROP_THRESHOLD_PERCENT, and "
                "REQUEST_TIMEOUT_SECONDS must be numeric"
            ) from exc

        if poll_interval < 1:
            raise ConfigurationError("POLL_INTERVAL_SECONDS must be at least 1")
        if drop_threshold < 0:
            raise ConfigurationError("DROP_THRESHOLD_PERCENT cannot be negative")
        if request_timeout < 1:
            raise ConfigurationError("REQUEST_TIMEOUT_SECONDS must be at least 1")

        odds_url = os.getenv("ODDS_API_URL", DEFAULT_ODDS_URL)
        sport = os.getenv("ODDS_SPORT", "soccer_epl")
        if "{sport}" in odds_url:
            odds_url = odds_url.format(sport=sport)

        return cls(
            odds_api_key=os.environ["ODDS_API_KEY"],
            telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
            telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"],
            odds_url=odds_url,
            sport=sport,
            regions=os.getenv("ODDS_REGIONS", "eu"),
            markets=os.getenv("ODDS_MARKETS", "h2h"),
            odds_format=os.getenv("ODDS_FORMAT", "decimal"),
            poll_interval=poll_interval,
            drop_threshold=drop_threshold,
            state_file=Path(os.getenv("ODDS_STATE_FILE", "odds_state.json")),
            request_timeout=request_timeout,
        )


def _text(value: Any, default: str = "Unknown") -> str:
    """Return a readable string for optional API fields."""

    if value is None or value == "":
        return default
    return str(value)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _normalise_status(value: Any) -> str:
    return _text(value, "").strip().lower().replace("-", "_").replace(" ", "_")


def _object_is_locked(*objects: dict[str, Any] | None) -> bool:
    """Inspect common explicit lock/status fields without guessing on absence."""

    status_keys = ("status", "market_status", "state")
    boolean_keys = ("locked", "is_locked", "suspended", "is_suspended")

    for obj in objects:
        if not isinstance(obj, dict):
            continue
        for key in status_keys:
            if _normalise_status(obj.get(key)) in LOCKED_STATUSES:
                return True
        for key in boolean_keys:
            if obj.get(key) is True:
                return True
        if obj.get("active") is False:
            return True
    return False


def _iter_offers(events: Any) -> Iterable[dict[str, Any]]:
    """Flatten a The Odds API response into comparable offer records."""

    if isinstance(events, dict):
        events = events.get("events", [])
    if not isinstance(events, list):
        raise ValueError("Odds API response must be a list of events")

    for event in events:
        if not isinstance(event, dict):
            continue
        event_id = _text(event.get("id"), _text(event.get("event_id")))
        event_name = _text(
            event.get("name"),
            f"{_text(event.get('home_team'))} vs {_text(event.get('away_team'))}",
        )
        bookmakers = event.get("bookmakers", [])
        if not isinstance(bookmakers, list):
            continue
        for bookmaker in bookmakers:
            if not isinstance(bookmaker, dict):
                continue
            bookmaker_id = _text(
                bookmaker.get("key"), _text(bookmaker.get("id"), _text(bookmaker.get("title")))
            )
            bookmaker_name = _text(bookmaker.get("title"), bookmaker_id)
            markets = bookmaker.get("markets", [])
            if not isinstance(markets, list):
                continue
            for market in markets:
                if not isinstance(market, dict):
                    continue
                market_id = _text(
                    market.get("key"), _text(market.get("id"), _text(market.get("name")))
                )
                market_name = _text(market.get("name"), market_id)
                locked = _object_is_locked(event, bookmaker, market)
                outcomes = market.get("outcomes", [])

                # A locked market may not have outcomes. Keep one record so the
                # lock transition can still be compared and alerted.
                if not isinstance(outcomes, list) or not outcomes:
                    yield {
                        "key": "|".join((event_id, bookmaker_id, market_id, "__market__")),
                        "event": event_name,
                        "bookmaker": bookmaker_name,
                        "market": market_name,
                        "outcome": "Market",
                        "point": None,
                        "price": None,
                        "locked": locked,
                    }
                    continue

                for outcome in outcomes:
                    if not isinstance(outcome, dict):
                        continue
                    outcome_name = _text(
                        outcome.get("name"), _text(outcome.get("label"), "Outcome")
                    )
                    point = outcome.get("point")
                    point_key = _text(point, "")
                    yield {
                        "key": "|".join(
                            (event_id, bookmaker_id, market_id, outcome_name, point_key)
                        ),
                        "event": event_name,
                        "bookmaker": bookmaker_name,
                        "market": market_name,
                        "outcome": outcome_name,
                        "point": point,
                        "price": _number(outcome.get("price")),
                        "locked": locked or _object_is_locked(outcome),
                    }


class OddsMonitor:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.session = requests.Session()

    def fetch_offers(self) -> list[dict[str, Any]]:
        response = self.session.get(
            self.config.odds_url,
            params={
                "apiKey": self.config.odds_api_key,
                "regions": self.config.regions,
                "markets": self.config.markets,
                "oddsFormat": self.config.odds_format,
            },
            timeout=self.config.request_timeout,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("Odds API returned invalid JSON") from exc
        return list(_iter_offers(payload))

    def send_telegram(self, message: str) -> None:
        url = (
            f"{DEFAULT_TELEGRAM_URL}/bot"
            f"{self.config.telegram_bot_token}/sendMessage"
        )
        response = self.session.post(
            url,
            data={
                "chat_id": self.config.telegram_chat_id,
                "text": message,
                "disable_web_page_preview": "true",
            },
            timeout=self.config.request_timeout,
        )
        response.raise_for_status()
        try:
            result = response.json()
        except ValueError as exc:
            raise ValueError("Telegram returned invalid JSON") from exc
        if not result.get("ok"):
            raise RuntimeError(f"Telegram rejected the message: {result}")

    def run_once(self) -> int:
        offers = self.fetch_offers()
        previous = load_state(self.config.state_file)
        current: dict[str, dict[str, Any]] = {}

        is_baseline = not previous
        if is_baseline:
            LOGGER.info("Loaded %d offers; saved the initial baseline", len(offers))
        alerts: list[str] = []

        for offer in offers:
            key = offer["key"]
            current[key] = {
                "price": offer["price"],
                "locked": offer["locked"],
            }
            old = previous.get(key, {})
            old_price = _number(old.get("price"))
            new_price = offer["price"]

            if (
                old_price is not None
                and new_price is not None
                and new_price < old_price
            ):
                drop_percent = (old_price - new_price) / old_price * 100
                if drop_percent > self.config.drop_threshold:
                    alerts.append(
                        format_drop_alert(
                            offer, old_price, drop_percent, self.config.drop_threshold
                        )
                    )

            if not is_baseline and offer["locked"] and not bool(old.get("locked")):
                alerts.append(format_lock_alert(offer))

        save_state(self.config.state_file, current)

        for alert in alerts:
            try:
                self.send_telegram(alert)
                LOGGER.info("Sent Telegram alert: %s", alert.splitlines()[0])
            except requests.RequestException:
                LOGGER.exception("Could not send a Telegram alert")
            except (RuntimeError, ValueError):
                LOGGER.exception("Telegram returned an unsuccessful response")

        LOGGER.info("Checked %d offers; generated %d alert(s)", len(offers), len(alerts))
        return len(alerts)


def format_drop_alert(
    offer: dict[str, Any],
    old_price: float,
    drop_percent: float,
    threshold: float,
) -> str:
    point = f" ({offer['point']})" if offer.get("point") is not None else ""
    return (
        "ODDS DROP\n"
        f"Event: {offer['event']}\n"
        f"Bookmaker: {offer['bookmaker']}\n"
        f"Market: {offer['market']}\n"
        f"Outcome: {offer['outcome']}{point}\n"
        f"Price: {old_price:.3f} -> {offer['price']:.3f}\n"
        f"Drop: {drop_percent:.2f}% (threshold: {threshold:g}%)"
    )


def format_lock_alert(offer: dict[str, Any]) -> str:
    return (
        "MARKET LOCKED\n"
        f"Event: {offer['event']}\n"
        f"Bookmaker: {offer['bookmaker']}\n"
        f"Market: {offer['market']}\n"
        f"Outcome: {offer['outcome']}"
    )


def load_state(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as file:
            state = json.load(file)
        prices = state.get("prices", state) if isinstance(state, dict) else {}
        return prices if isinstance(prices, dict) else {}
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Ignoring unreadable state file %s: %s", path, exc)
        return {}


def save_state(path: Path, state: dict[str, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"prices": state}, indent=2, sort_keys=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as file:
            file.write(payload)
            file.flush()
            temporary_name = file.name
        os.replace(temporary_name, path)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="poll once and exit instead of running continuously",
    )
    parser.add_argument(
        "--log-level",
        default=os.getenv("LOG_LEVEL", "INFO"),
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="logging verbosity (default: INFO)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        config = Config.from_environment()
        monitor = OddsMonitor(config)
    except ConfigurationError as exc:
        LOGGER.error("%s", exc)
        return 2

    start_health_server()
    LOGGER.info("Health server listening on port %d", HEALTH_PORT)

    while True:
        try:
            monitor.run_once()
        except requests.RequestException as exc:
            LOGGER.error("Odds API request failed: %s", exc)
        except (ValueError, RuntimeError) as exc:
            LOGGER.error("Odds poll failed: %s", exc)

        if args.once:
            return 0
        try:
            time.sleep(config.poll_interval)
        except KeyboardInterrupt:
            LOGGER.info("Stopping odds monitor")
            return 0


if __name__ == "__main__":
    sys.exit(main())