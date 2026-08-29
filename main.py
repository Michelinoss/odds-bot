#!/usr/bin/env python3
"""Monitor bet365 pre-match soccer odds across every available league.

The first successful poll after process startup silently replaces the local
baseline and never sends alerts. Later polls send Telegram alerts for odds
drops greater than the configured threshold or explicit market locks.

The script discovers active soccer leagues from The Odds API, requests only
standard ``h2h`` back odds, and ignores every bookmaker except bet365.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
import os
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import requests


LOGGER = logging.getLogger("odds-monitor")
DEFAULT_SPORTS_URL = "https://football-odds-scraper.p.rapidapi.com/v1/sports"
DEFAULT_ODDS_URL = "https://football-odds-scraper.p.rapidapi.com/v1/odds/{sport}"
DEFAULT_TELEGRAM_URL = "https://api.telegram.org"
TARGET_BOOKMAKER = "bet365"
STANDARD_MARKET = "h2h"
LOCKED_STATUSES = {
    "closed",
    "halted",
    "inactive",
    "locked",
    "offline",
    "suspended",
    "unavailable",
}
PREMATCH_EXCLUDED_STATUSES = {
    "cancelled",
    "canceled",
    "completed",
    "finished",
    "inplay",
    "live",
    "postponed",
    "started",
}


class ConfigurationError(ValueError):
    """Raised when required environment configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    odds_api_key: str
    telegram_bot_token: str
    telegram_chat_id: str
    sports_url: str
    odds_url: str
    regions: str
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

        return cls(
            odds_api_key=os.environ["ODDS_API_KEY"],
            telegram_bot_token=os.environ["TELEGRAM_BOT_TOKEN"],
            telegram_chat_id=os.environ["TELEGRAM_CHAT_ID"],
            sports_url=os.getenv("ODDS_SPORTS_URL", DEFAULT_SPORTS_URL),
            odds_url=os.getenv("ODDS_API_URL", DEFAULT_ODDS_URL),
            regions=os.getenv("ODDS_REGIONS", "eu"),
            poll_interval=poll_interval,
            drop_threshold=drop_threshold,
            state_file=Path(os.getenv("ODDS_STATE_FILE", "odds_state.json")),
            request_timeout=request_timeout,
        )


def _text(value: Any, default: str = "Unknown") -> str:
    if value is None or value == "":
        return default
    return str(value)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _normalise(value: Any) -> str:
    return "".join(character.lower() for character in _text(value, "") if character.isalnum())


def _normalise_status(value: Any) -> str:
    return _text(value, "").strip().lower().replace("-", "_").replace(" ", "_")


def _request_error_summary(error: requests.RequestException) -> str:
    response = getattr(error, "response", None)
    if response is not None:
        return f"HTTP {response.status_code}"
    return type(error).__name__


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _is_prematch(event: dict[str, Any], now: datetime) -> bool:
    status = _normalise_status(
        event.get("status") or event.get("event_status") or event.get("state")
    )
    if status in PREMATCH_EXCLUDED_STATUSES:
        return False

    commence_time = _parse_datetime(
        event.get("commence_time")
        or event.get("start_time")
        or event.get("startTime")
    )
    if commence_time is None:
        return False
    return commence_time.astimezone(timezone.utc) > now


def _object_is_locked(*objects: dict[str, Any] | None) -> bool:
    status_keys = ("status", "market_status", "state")
    boolean_keys = ("locked", "is_locked", "suspended", "is_suspended")

    for obj in objects:
        if not isinstance(obj, dict):
            continue
        if any(
            _normalise_status(obj.get(key)) in LOCKED_STATUSES for key in status_keys
        ):
            return True
        if any(obj.get(key) is True for key in boolean_keys):
            return True
        if obj.get("active") is False:
            return True
    return False


def _is_soccer_sport(sport: dict[str, Any]) -> bool:
    key = _text(sport.get("key"), "").lower()
    return (key == "soccer" or key.startswith("soccer_")) and (
        sport.get("active") is not False
    )


def _iter_offers(
    events: Any,
    sport_key: str,
    sport_title: str,
    now: datetime,
) -> Iterable[dict[str, Any]]:
    if isinstance(events, dict):
        events = events.get("events", [])
    if not isinstance(events, list):
        raise ValueError("Odds API response must be a list of events")

    for event in events:
        if not isinstance(event, dict) or not _is_prematch(event, now):
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
            bookmaker_key = _normalise(bookmaker.get("key"))
            bookmaker_title = _normalise(bookmaker.get("title"))
            if bookmaker_key != TARGET_BOOKMAKER and bookmaker_title != TARGET_BOOKMAKER:
                continue

            bookmaker_name = _text(bookmaker.get("title"), TARGET_BOOKMAKER)
            markets = bookmaker.get("markets", [])
            if not isinstance(markets, list):
                continue

            for market in markets:
                if not isinstance(market, dict):
                    continue
                market_key = _text(
                    market.get("key"),
                    _text(market.get("id"), _text(market.get("name"))),
                ).lower()
                if market_key != STANDARD_MARKET:
                    continue

                market_name = _text(market.get("name"), market_key)
                locked = _object_is_locked(event, bookmaker, market)
                outcomes = market.get("outcomes", [])
                if not isinstance(outcomes, list) or not outcomes:
                    yield {
                        "key": "|".join(
                            (sport_key, event_id, TARGET_BOOKMAKER, market_key, "__market__")
                        ),
                        "sport": sport_title,
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
                    yield {
                        "key": "|".join(
                            (
                                sport_key,
                                event_id,
                                TARGET_BOOKMAKER,
                                market_key,
                                outcome_name,
                                _text(point, ""),
                            )
                        ),
                        "sport": sport_title,
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
        self._baseline_loaded = False
        self._last_fetch_complete = True

    def fetch_soccer_sports(self) -> list[dict[str, Any]]:
        response = self.session.get(
            self.config.sports_url,
            params={"apiKey": self.config.odds_api_key},
            timeout=self.config.request_timeout,
        )
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError("Sports endpoint returned invalid JSON") from exc
        if not isinstance(payload, list):
            raise ValueError("Sports endpoint response must be a list")
        return [sport for sport in payload if isinstance(sport, dict) and _is_soccer_sport(sport)]

    def fetch_offers(self) -> list[dict[str, Any]]:
        try:
            sports = self.fetch_soccer_sports()
        except requests.RequestException as exc:
            self._last_fetch_complete = False
            LOGGER.error("Could not discover soccer leagues: %s", _request_error_summary(exc))
            return []
        except ValueError as exc:
            self._last_fetch_complete = False
            LOGGER.error("Could not parse soccer league list: %s", exc)
            return []

        now = datetime.now(timezone.utc)
        offers: list[dict[str, Any]] = []
        self._last_fetch_complete = True
        LOGGER.info("Discovered %d active soccer leagues", len(sports))

        for sport in sports:
            sport_key = _text(sport.get("key"), "")
            try:
                odds_url = self.config.odds_url.format(sport=sport_key)
                response = self.session.get(
                    odds_url,
                    params={
                        "apiKey": self.config.odds_api_key,
                        "regions": self.config.regions,
                        "markets": STANDARD_MARKET,
                        "oddsFormat": "decimal",
                    },
                    timeout=self.config.request_timeout,
                )
                response.raise_for_status()
                payload = response.json()
                offers.extend(
                    _iter_offers(
                        payload,
                        sport_key,
                        _text(sport.get("title"), sport_key),
                        now,
                    )
                )
            except requests.RequestException as exc:
                self._last_fetch_complete = False
                LOGGER.warning(
                    "Could not fetch %s odds: %s",
                    sport_key,
                    _request_error_summary(exc),
                )
            except ValueError as exc:
                LOGGER.warning("Could not parse %s odds: %s", sport_key, exc)

        return offers

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
        if not self._last_fetch_complete:
            LOGGER.warning("Skipping alerts and state update because the odds poll was incomplete")
            return 0

        previous = load_state(self.config.state_file)
        is_baseline = not self._baseline_loaded
        if is_baseline:
            LOGGER.info("Loaded %d bet365 pre-match offers as the silent baseline", len(offers))

        current: dict[str, dict[str, Any]] = {}
        alerts: list[str] = []
        for offer in offers:
            key = offer["key"]
            current[key] = {"price": offer["price"], "locked": offer["locked"]}
            old = previous.get(key, {})
            old_price = _number(old.get("price"))
            new_price = offer["price"]

            if (
                not is_baseline
                and old_price is not None
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
        self._baseline_loaded = True

        for alert in alerts:
            try:
                self.send_telegram(alert)
                LOGGER.info("Sent Telegram alert: %s", alert.splitlines()[0])
            except requests.RequestException:
                LOGGER.exception("Could not send a Telegram alert")
            except (RuntimeError, ValueError):
                LOGGER.exception("Telegram returned an unsuccessful response")

        LOGGER.info("Checked %d eligible bet365 offers; generated %d alert(s)", len(offers), len(alerts))
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
        f"League: {offer['sport']}\n"
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
        f"League: {offer['sport']}\n"
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

    while True:
        try:
            monitor.run_once()
        except requests.RequestException as exc:
            LOGGER.error("Odds API request failed: %s", _request_error_summary(exc))
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
from flask import Flask
import threading

app = Flask(__name__)

@app.route('/')
def health():
    return "OK", 200

def keep_alive():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=keep_alive, daemon=True).start()
