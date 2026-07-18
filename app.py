#!/usr/bin/env python3
"""Monitor District movie listings and notify when a new show appears."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen


API_URL = "https://www.district.in/gw/consumer/movies/v5/movie"
SHOWTIME_KEYS = {"showtimes", "show_times", "shows", "sessions", "timings", "showtime"}
VENUE_KEYS = {"cinema_name", "cinemaname", "venue_name", "venuename", "theatre_name", "theatrename"}


@dataclass(frozen=True)
class Listing:
    venue: str
    showtimes: tuple[str, ...]
    screen_format: str = "Standard"


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Configuration file not found: {path}")
    except json.JSONDecodeError as error:
        raise SystemExit(f"Invalid JSON in {path}: {error}")


def api_parameters(watch: dict[str, Any]) -> dict[str, str]:
    source = urlparse(watch["district_url"])
    query = parse_qs(source.query)
    movie_code = query.get("frmtid", [None])[0]
    if not movie_code:
        raise ValueError("district_url must contain a frmtid query parameter")

    path_parts = source.path.rstrip("/").split("-")
    content_id = next((part.removeprefix("MV") for part in reversed(path_parts) if part.startswith("MV")), None)
    if not content_id:
        raise ValueError("district_url path must end with a movie ID such as -MV187151")

    return {
        "version": "3",
        "site_id": "1",
        "channel": "web",
        "child_site_id": "1",
        "platform": "district",
        "movieCode": movie_code,
        "city_key": watch["city_key"],
        "content_id": content_id,
        "date": watch["date"],
        "latitude": str(watch["latitude"]),
        "longitude": str(watch["longitude"]),
        "cinemaOrderLogic": "3",
    }


def fetch_listing(watch: dict[str, Any]) -> dict[str, Any]:
    from urllib.parse import urlencode

    request = Request(
        f"{API_URL}?{urlencode(api_parameters(watch))}",
        headers={
            "Accept": "application/json, text/plain, */*",
            "api_source": "district",
            "Referer": watch["district_url"],
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            "x-app-type": "ed_web",
            "x-client-id": "district-web",
            "x-app-version": "11.11.1",
            "x-guest-token": f"{int(time.time() * 1000)}_{secrets.randbelow(10**18)}_{secrets.token_hex(16)}",
            "x-request-id": str(uuid.uuid4()),
        },
    )
    try:
        with urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise RuntimeError(f"District returned HTTP {error.code}. The anonymous token is generated automatically; do not add browser cookies to the config.") from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Unable to retrieve District listings: {error}") from error


def strings(value: Any) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if isinstance(value, list):
        return [text for item in value for text in strings(item)]
    if isinstance(value, dict):
        preferred = ("time", "show_time", "start_time", "display_time", "label", "name")
        return [str(value[key]).strip() for key in preferred if value.get(key) not in (None, "")]
    return []


def find_listings(node: Any, inherited_venue: str = "Unknown venue") -> list[Listing]:
    if isinstance(node, list):
        return [listing for item in node for listing in find_listings(item, inherited_venue)]
    if not isinstance(node, dict):
        return []

    normalized = {str(key).lower(): value for key, value in node.items()}
    venue = next((str(value).strip() for key, value in normalized.items() if key in VENUE_KEYS and value), inherited_venue)
    listings: list[Listing] = []
    for key, value in normalized.items():
        if key in SHOWTIME_KEYS:
            times = tuple(dict.fromkeys(strings(value)))
            if times:
                listings.append(Listing(venue, times))
    for value in node.values():
        listings.extend(find_listings(value, venue))
    return listings


def summarize(payload: dict[str, Any]) -> list[Listing]:
    district_listings: list[Listing] = []
    page_data = payload.get("pageData", {})
    screen_formats = json.loads(payload.get("meta", {}).get("entityMetaData", {}).get("screen_format_dict", "{}"))
    for cinema in page_data.get("nearbyCinemas", []) + page_data.get("farCinemas", []):
        venue = cinema.get("cinemaInfo", {}).get("name", "Unknown venue")
        cinema_formats = screen_formats.get(str(cinema.get("id")), [])
        sessions_by_format: dict[str, list[str]] = {}
        for session in cinema.get("sessions", []):
            showtime = session.get("showTime")
            if not showtime:
                continue
            explicit_format = next((session.get(key) for key in ("screenFormat", "screen_format", "format", "experience") if session.get(key)), None)
            audio = str(session.get("audi", ""))
            detected = explicit_format or (audio if "imax" in audio.lower() or "4dx" in audio.lower() else None)
            detected = detected or (cinema_formats[0] if len(cinema_formats) == 1 else "Standard")
            normalized_format = normalize_screen_format(str(detected))
            sessions_by_format.setdefault(normalized_format, []).append(showtime)
        district_listings.extend(Listing(venue, tuple(times), screen_format) for screen_format, times in sessions_by_format.items())
    if district_listings:
        return district_listings

    unique = {(listing.venue, listing.showtimes) for listing in find_listings(payload)}
    return [Listing(venue, times) for venue, times in sorted(unique)]


def normalize_screen_format(value: str) -> str:
    lowered = value.lower()
    if "imax" in lowered:
        return "IMAX"
    if "4dx" in lowered:
        return "4DX"
    return value.strip() or "Standard"


def load_state(path: Path) -> dict[str, Any]:
    return load_json(path) if path.exists() else {}


def send_discord(webhook: str, watch: dict[str, Any], listings: list[Listing]) -> None:
    send_discord_text(webhook, format_listing_report(watch, listings))


def format_listing_report(watch: dict[str, Any], listings: list[Listing]) -> str:
    lines = [f"**{watch['name']} — {watch['date']}**", f"City: {watch['city_key'].title()}"]
    grouped: dict[str, list[Listing]] = {}
    for listing in listings:
        grouped.setdefault(listing.screen_format, []).append(listing)
    if not grouped:
        lines.append("No showtimes are listed yet.")
    for screen_format in sorted(grouped):
        lines.append(f"\n**{screen_format}**")
        for listing in sorted(grouped[screen_format], key=lambda item: item.venue):
            lines.append(f"• **{listing.venue}**: {', '.join(listing.showtimes)}")
    lines.append(f"\n{watch['district_url']}")
    message = "\n".join(lines)
    return message if len(message) <= 1900 else f"{message[:1890]}\n…"


def send_discord_text(webhook: str, message: str, mention: str = "") -> None:
    content = f"{mention}\n{message}".strip()
    payload: dict[str, Any] = {"content": content}
    if mention:
        payload["allowed_mentions"] = {"parse": ["users", "roles", "everyone"]}
    body = json.dumps(payload).encode()
    request = Request(webhook, data=body, headers={"Content-Type": "application/json", "User-Agent": "show-listing-monitor/1.0"})
    with urlopen(request, timeout=20):
        pass


def run(config: dict[str, Any], state_path: Path, notify: bool) -> int:
    state = load_state(state_path)
    updated_state: dict[str, Any] = {}
    for watch in config["watches"]:
        payload = fetch_listing(watch)
        listings = summarize(payload)
        signature = hashlib.sha256(json.dumps([(item.venue, item.showtimes) for item in listings]).encode()).hexdigest()
        previous = state.get(watch["name"], {})
        is_new_listing = bool(listings) and previous.get("signature") != signature
        print(f"{watch['name']}: {len(listings)} listing group(s)")
        if is_new_listing and previous:
            webhook = os.getenv("DISCORD_WEBHOOK_URL")
            if notify and webhook:
                send_discord(webhook, watch, listings)
                print("  Notification sent.")
            elif notify:
                print("  Listing changed; set DISCORD_WEBHOOK_URL to receive alerts.")
        updated_state[watch["name"]] = {"signature": signature, "checked_at": datetime.now(timezone.utc).isoformat()}
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(updated_state, indent=2) + "\n", encoding="utf-8")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--state", default="state/state.json")
    parser.add_argument("--no-notify", action="store_true")
    args = parser.parse_args()
    try:
        return run(load_json(Path(args.config)), Path(args.state), not args.no_notify)
    except (KeyError, TypeError, ValueError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
