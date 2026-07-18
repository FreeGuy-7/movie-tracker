#!/usr/bin/env python3
"""Monitor District movie listings and notify when a new show appears."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import ssl
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

from settings import load_environment

try:
    import certifi
except ImportError:
    certifi = None


load_environment()


DISTRICT_API_URL = "https://www.district.in/gw/consumer/movies/v5/movie"
PVR_API_URL = "https://api3.pvrcinemas.com/api/v1/booking/content/msessions"
IST = timezone(timedelta(hours=5, minutes=30), name="IST")
TLS_CONTEXT = ssl.create_default_context(cafile=certifi.where()) if certifi else ssl.create_default_context()
SHOWTIME_KEYS = {"showtimes", "show_times", "shows", "sessions", "timings", "showtime"}
VENUE_KEYS = {"cinema_name", "cinemaname", "venue_name", "venuename", "theatre_name", "theatrename"}


def debug_log(event: str, **details: Any) -> None:
    path = os.getenv("DEBUG_LOG_PATH", "").strip()
    if not path:
        return
    try:
        record = {"time": datetime.now(timezone.utc).isoformat(), "event": event, **details}
        log_path = Path(path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, default=str) + "\n")
    except OSError:
        pass


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
    source = urlparse(movie_url(watch))
    query = parse_qs(source.query)
    movie_code = query.get("frmtid", [None])[0]
    if not movie_code:
        raise ValueError("district_url must contain a frmtid query parameter")

    path_parts = source.path.rstrip("/").split("-")
    content_id = next((part[2:] for part in reversed(path_parts) if part.startswith("MV")), None)
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


def movie_url(watch: dict[str, Any]) -> str:
    provider = watch.get("provider", "district")
    return watch.get("source_url") or watch.get(f"{provider}_url") or watch["district_url"]


def pvr_parameters(watch: dict[str, Any]) -> dict[str, Any]:
    source = urlparse(movie_url(watch))
    parts = [unquote(part) for part in source.path.split("/") if part]
    if len(parts) < 4 or not parts[-1].isdigit():
        raise ValueError("PVR URL must end with its numeric movie ID, such as /35098")
    return {
        "city": watch.get("city_name") or parts[-3],
        "mid": parts[-1],
        "experience": watch.get("experience", "ALL"),
        "specialTag": "ALL",
        "lat": str(watch["latitude"]),
        "lng": str(watch["longitude"]),
        "lang": "ALL",
        "format": "ALL",
        "dated": watch["date"],
        "time": "08:00-24:00",
        "cinetype": "ALL",
        "hc": "ALL",
        "adFree": False,
        "bbt": False,
    }


def fetch_listing(watch: dict[str, Any]) -> dict[str, Any]:
    provider = watch.get("provider", "district")
    if provider == "pvr":
        return fetch_pvr_listing(watch)
    if provider != "district":
        raise ValueError(f"Unsupported provider: {provider}")
    return fetch_district_listing(watch)


def fetch_district_listing(watch: dict[str, Any]) -> dict[str, Any]:
    from urllib.parse import urlencode

    request = Request(
        f"{DISTRICT_API_URL}?{urlencode(api_parameters(watch))}",
        headers={
            "Accept": "application/json, text/plain, */*",
            "api_source": "district",
            "Referer": movie_url(watch),
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
            "x-app-type": "ed_web",
            "x-client-id": "district-web",
            "x-app-version": "11.11.1",
            "x-guest-token": f"{int(time.time() * 1000)}_{secrets.randbelow(10**18)}_{secrets.token_hex(16)}",
            "x-request-id": str(uuid.uuid4()),
        },
    )
    try:
        with urlopen(request, timeout=20, context=TLS_CONTEXT) as response:
            payload = json.loads(response.read().decode("utf-8"))
            debug_log("district_response", date=watch.get("date"), city=watch.get("city_key"), status=response.status, root_keys=sorted(payload) if isinstance(payload, dict) else type(payload).__name__)
            return payload
    except HTTPError as error:
        debug_log("district_http_error", date=watch.get("date"), city=watch.get("city_key"), status=error.code)
        raise RuntimeError(f"District returned HTTP {error.code}. The anonymous token is generated automatically; do not add browser cookies to the config.") from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        debug_log("district_request_error", date=watch.get("date"), city=watch.get("city_key"), error=str(error))
        raise RuntimeError(f"Unable to retrieve District listings: {error}") from error


def fetch_pvr_listing(watch: dict[str, Any]) -> dict[str, Any]:
    parameters = pvr_parameters(watch)
    body = json.dumps(parameters).encode()
    request = Request(
        PVR_API_URL,
        data=body,
        headers={
            "Accept": "application/json, text/plain, */*",
            "appversion": "1.0",
            "authorization": "Bearer",
            "chain": "PVR",
            "city": str(parameters["city"]),
            "content-type": "application/json",
            "country": "INDIA",
            "origin": "https://www.pvrcinemas.com",
            "platform": "WEBSITE",
            "Referer": movie_url(watch),
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=20, context=TLS_CONTEXT) as response:
            payload = json.loads(response.read().decode("utf-8"))
            output = payload.get("output") if isinstance(payload, dict) else None
            sessions = output.get("movieCinemaSessions") if isinstance(output, dict) else None
            debug_log("pvr_response", date=watch.get("date"), city=parameters["city"], movie_id=parameters["mid"], status=response.status, api_message=payload.get("msg") if isinstance(payload, dict) else None, session_count=len(sessions or []) if isinstance(sessions, list) else 0)
            return payload
    except HTTPError as error:
        debug_log("pvr_http_error", date=watch.get("date"), city=parameters["city"], movie_id=parameters["mid"], status=error.code)
        raise RuntimeError(f"PVR returned HTTP {error.code}.") from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        debug_log("pvr_request_error", date=watch.get("date"), city=parameters["city"], movie_id=parameters["mid"], error=str(error))
        if certifi is None and "CERTIFICATE_VERIFY_FAILED" in str(error):
            raise RuntimeError("PVR HTTPS verification failed. Install dependencies with: python3 -m pip install -r requirements.txt") from error
        raise RuntimeError(f"Unable to retrieve PVR listings: {error}") from error


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


def summarize(payload: dict[str, Any], watch: dict[str, Any] | None = None) -> list[Listing]:
    watch = watch or {}
    if watch.get("provider") == "pvr":
        return summarize_pvr(payload, watch.get("experience", "ALL"))
    return summarize_district(payload, watch.get("experience", "ALL"))


def summarize_district(payload: dict[str, Any], experience: str) -> list[Listing]:
    district_listings: list[Listing] = []
    page_data = payload.get("pageData", {})
    if not isinstance(page_data, dict):
        return []
    raw_screen_formats = payload.get("meta", {}).get("entityMetaData", {}).get("screen_format_dict", "{}")
    try:
        screen_formats = json.loads(raw_screen_formats) if isinstance(raw_screen_formats, str) else (raw_screen_formats or {})
    except json.JSONDecodeError:
        screen_formats = {}
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
            if not matches_experience(normalized_format, experience):
                continue
            sessions_by_format.setdefault(normalized_format, []).append(showtime)
        district_listings.extend(Listing(venue, tuple(times), screen_format) for screen_format, times in sessions_by_format.items())
    if district_listings:
        return district_listings

    unique = {(listing.venue, listing.showtimes) for listing in find_listings(payload)}
    return [Listing(venue, times) for venue, times in sorted(unique) if matches_experience("Standard", experience)]


def summarize_pvr(payload: dict[str, Any], experience: str) -> list[Listing]:
    listings: list[Listing] = []
    output = payload.get("output")
    if not isinstance(output, dict):
        return []
    cinemas = output.get("movieCinemaSessions") or []
    for cinema_session in cinemas:
        if not isinstance(cinema_session, dict):
            continue
        cinema = cinema_session.get("cinema") or {}
        venue = cinema.get("name", "Unknown venue") if isinstance(cinema, dict) else "Unknown venue"
        grouped: dict[str, list[str]] = {}
        for experience_session in cinema_session.get("experienceSessions") or []:
            if not isinstance(experience_session, dict):
                continue
            experience_name = str(experience_session.get("experience", "Standard"))
            for show in experience_session.get("shows") or []:
                if not isinstance(show, dict):
                    continue
                screen_format = normalize_screen_format(str(show.get("screenType") or show.get("filmFormat") or experience_name))
                if matches_experience(f"{experience_name} {screen_format}", experience):
                    showtime = show.get("showTime")
                    if showtime:
                        grouped.setdefault(screen_format, []).append(str(showtime))
        listings.extend(Listing(venue, tuple(times), screen_format) for screen_format, times in grouped.items())
    return listings


def normalize_screen_format(value: str) -> str:
    lowered = value.lower()
    if "imax" in lowered:
        return "IMAX"
    if "4dx" in lowered:
        return "4DX"
    return value.strip() or "Standard"


def matches_experience(value: str, selected: str) -> bool:
    return selected.upper() == "ALL" or selected.lower() in value.lower()


def load_state(path: Path) -> dict[str, Any]:
    return load_json(path) if path.exists() else {}


def send_discord(webhook: str, watch: dict[str, Any], listings: list[Listing]) -> None:
    send_discord_text(webhook, format_listing_report(watch, listings))


def format_listing_report(watch: dict[str, Any], listings: list[Listing]) -> str:
    showtime_count = sum(len(listing.showtimes) for listing in listings)
    cinema_count = len({listing.venue for listing in listings})
    lines = [
        f"🎬 **{watch['name']}**",
        f"📅 {format_date(watch['date'])} · 📍 {watch['city_key'].title()}",
        f"{cinema_count} cinema{'s' if cinema_count != 1 else ''} · {showtime_count} showtime{'s' if showtime_count != 1 else ''}",
    ]
    grouped: dict[str, list[Listing]] = {}
    for listing in listings:
        grouped.setdefault(listing.screen_format, []).append(listing)
    if not grouped:
        lines.append("\nNo showtimes are listed yet.")
    for screen_format in sorted(grouped):
        lines.append(f"\n━━ **{screen_format.upper()}** ━━")
        for listing in sorted(grouped[screen_format], key=lambda item: item.venue):
            times = " · ".join(format_showtime(showtime) for showtime in listing.showtimes)
            lines.append(f"**{listing.venue}**\n↳ {times}")
    if not listings and watch.get("provider") == "pvr":
        lines.append("\nPVR has not published sessions for this date yet. The trigger will keep checking automatically.")
    lines.append(f"\n🔗 {movie_url(watch)}")
    return "\n".join(lines)


def format_date(value: str) -> str:
    try:
        return datetime.fromisoformat(value).strftime("%a, %-d %b %Y")
    except ValueError:
        return value


def format_showtime(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        local = parsed.astimezone(IST)
        hour = local.hour % 12 or 12
        period = "AM" if local.hour < 12 else "PM"
        return f"{hour}:{local.minute:02d} {period} IST"
    except ValueError:
        return value if value.endswith("IST") else f"{value} IST"


def send_discord_text(webhook: str, message: str, mention: str = "") -> None:
    for index, chunk in enumerate(discord_chunks(message)):
        content = f"{mention}\n{chunk}".strip() if index == 0 else chunk
        payload: dict[str, Any] = {"content": content}
        if mention and index == 0:
            payload["allowed_mentions"] = {"parse": ["users", "roles", "everyone"]}
        body = json.dumps(payload).encode()
        request = Request(webhook, data=body, headers={"Content-Type": "application/json", "User-Agent": "show-listing-monitor/1.0"})
        with urlopen(request, timeout=20, context=TLS_CONTEXT):
            pass


def discord_chunks(message: str, limit: int = 1900) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in message.splitlines():
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit and current:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or ["No report content available."]


def run(config: dict[str, Any], state_path: Path, notify: bool) -> int:
    state = load_state(state_path)
    updated_state: dict[str, Any] = {}
    for watch in config["watches"]:
        payload = fetch_listing(watch)
        listings = summarize(payload, watch)
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
