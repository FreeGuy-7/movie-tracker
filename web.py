#!/usr/bin/env python3
"""Small web dashboard and continuous scheduler for District movie watches."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app import IST, Listing, debug_log, discord_mention, fetch_listing, format_listing_report, send_discord_text, summarize


DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
TRIGGERS_PATH = DATA_DIR / "triggers.json"
STATE_PATH = DATA_DIR / "state.json"
LOCK = threading.Lock()
LAST_HEARTBEAT = 0.0


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load(path: Path, default: object) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def triggers() -> list[dict]:
    items = load(TRIGGERS_PATH, [])  # type: ignore[assignment]
    active = [item for item in items if not trigger_expired(item)]
    if len(active) != len(items):
        save_triggers(active)
        debug_log("expired_triggers_removed", removed=len(items) - len(active))
    return active  # type: ignore[return-value]


def save_triggers(items: list[dict]) -> None:
    save(TRIGGERS_PATH, items)


def trigger_expired(trigger: dict, current_date: date | None = None) -> bool:
    try:
        target_date = date.fromisoformat(str(trigger["date"]))
    except (KeyError, TypeError, ValueError):
        return False
    return target_date < (current_date or datetime.now(IST).date())


def trigger_dates(start_value: str, end_value: str) -> list[str]:
    start = date.fromisoformat(start_value)
    end = date.fromisoformat(end_value)
    if end < start:
        raise ValueError("End date must be on or after the start date")
    if (end - start).days > 366:
        raise ValueError("Date range cannot exceed 367 days")
    return [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]


def bootstrap() -> None:
    if TRIGGERS_PATH.exists():
        return
    config = load(Path("config.json"), {"watches": []})
    initial = []
    for watch in config.get("watches", []):
        initial.append({**watch, "id": str(uuid.uuid4()), "frequency_minutes": 120, "last_checked_at": None, "last_error": None})
    save_triggers(initial)


def listing_signature(listings: list[Listing]) -> str:
    return hashlib.sha256(json.dumps(listing_items(listings)).encode()).hexdigest()


def listing_items(listings: list[Listing]) -> list[tuple[str, str, str]]:
    return sorted((listing.screen_format, listing.venue, showtime) for listing in listings for showtime in listing.showtimes)


def check(trigger: dict) -> None:
    state = load(STATE_PATH, {})
    try:
        listings = summarize(fetch_listing(trigger), trigger)
        debug_log("trigger_success", trigger_id=trigger.get("id"), provider=trigger.get("provider"), name=trigger.get("name"), date=trigger.get("date"), listing_groups=len(listings), showtimes=sum(len(item.showtimes) for item in listings))
        signature = listing_signature(listings)
        previous = state.get(trigger["id"], {})
        current_items = listing_items(listings)
        previous_items = {tuple(item) for item in previous.get("shows", [])}
        new_items = [item for item in current_items if item not in previous_items]
        run_webhook = os.getenv("DISCORD_TRIGGER_WEBHOOK_URL")
        if run_webhook:
            send_discord_text(run_webhook, format_listing_report(trigger, listings))
        new_show_webhook = os.getenv("DISCORD_NEW_SHOW_WEBHOOK_URL")
        if new_show_webhook and previous and new_items:
            examples = "\n".join(f"• {screen_format} — {venue}: {showtime}" for screen_format, venue, showtime in new_items[:10])
            suffix = "\n…" if len(new_items) > 10 else ""
            send_discord_text(new_show_webhook, f"🚨 **New showtime{'s' if len(new_items) > 1 else ''} added for {trigger['name']}**\n{examples}{suffix}", discord_mention())
        state[trigger["id"]] = {"signature": signature, "shows": current_items, "checked_at": now()}
        trigger["last_error"] = None
    except Exception as error:
        trigger["last_error"] = str(error)
        debug_log("trigger_error", trigger_id=trigger.get("id"), provider=trigger.get("provider"), name=trigger.get("name"), date=trigger.get("date"), error=str(error))
    finally:
        trigger["last_checked_at"] = now()
        save(STATE_PATH, state)


def due(trigger: dict, timestamp: float) -> bool:
    previous = trigger.get("last_checked_at")
    if not previous:
        return True
    try:
        checked = datetime.fromisoformat(previous).timestamp()
        return timestamp - checked >= int(trigger["frequency_minutes"]) * 60
    except (KeyError, TypeError, ValueError):
        return True


def run_due() -> None:
    if not LOCK.acquire(blocking=False):
        return
    try:
        items = triggers()
        for trigger in items:
            if due(trigger, time.time()):
                check(trigger)
        save_triggers(items)
    finally:
        LOCK.release()


def send_heartbeat() -> None:
    global LAST_HEARTBEAT
    interval = max(1, int(os.getenv("HEARTBEAT_MINUTES", "60"))) * 60
    if time.time() - LAST_HEARTBEAT < interval:
        return
    webhook = os.getenv("DISCORD_STATUS_WEBHOOK_URL")
    if not webhook:
        return
    items = triggers()
    errors = sum(bool(item.get("last_error")) for item in items)
    try:
        send_discord_text(webhook, f"✅ **Show Watcher is running**\nActive triggers: {len(items)}\nTriggers with errors: {errors}\nUTC: {now()}")
        LAST_HEARTBEAT = time.time()
    except Exception as error:
        print(f"Heartbeat failed: {error}")


def scheduler() -> None:
    debug_log("scheduler_started", data_dir=str(DATA_DIR))
    while True:
        run_due()
        send_heartbeat()
        time.sleep(15)


def page(items: list[dict], notice: str = "") -> str:
    rows = "".join(
        f"<tr><td><strong>{html.escape(item['name'])}</strong><br><small>{html.escape(item.get('provider', 'district').upper())} · {html.escape(item.get('experience', 'ALL'))} · {html.escape(item['date'])} · every {item['frequency_minutes']} min</small></td>"
        f"<td>{html.escape(item['city_key'].title())}</td><td>{html.escape(item.get('last_checked_at') or 'Not checked')}</td>"
        f"<td>{html.escape(item.get('last_error') or 'Healthy')}</td><td><form method='post' action='/delete'><input type='hidden' name='id' value='{item['id']}'><button class='quiet'>Delete</button></form></td></tr>"
        for item in items
    ) or "<tr><td colspan='5'>No triggers yet.</td></tr>"
    return f"""<!doctype html><html><head><meta name=viewport content='width=device-width,initial-scale=1'>
<title>Show Watcher</title><style>
body{{max-width:960px;margin:40px auto;padding:0 18px;font:16px system-ui;color:#172033;background:#f7f8fc}}h1{{margin-bottom:4px}}.card{{background:white;border:1px solid #e4e7ef;border-radius:12px;padding:22px;margin:22px 0;box-shadow:0 1px 3px #00000008}}form.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}label{{font-size:13px;font-weight:650;display:grid;gap:5px}}input,select{{padding:10px;border:1px solid #b9c0cf;border-radius:7px;font:inherit}}.wide{{grid-column:1/-1}}fieldset{{border:0;padding:0;margin:0}}.platforms{{display:flex;gap:18px;margin-top:7px}}.platforms label{{display:flex;align-items:center;gap:7px;font-size:15px}}.platforms input{{padding:0}}#city-custom{{display:none}}#location-note{{color:#667085;font-size:13px}}button{{background:#3b5bdb;color:white;border:0;border-radius:7px;padding:10px 14px;font-weight:650;cursor:pointer}}button.quiet{{background:#fff;color:#b42318;border:1px solid #f3c7c4;padding:7px 10px}}table{{width:100%;border-collapse:collapse}}td,th{{text-align:left;padding:12px 8px;border-bottom:1px solid #e8eaf0;vertical-align:top}}small{{color:#667085}}.notice{{color:#067647;font-weight:650}}</style></head><body>
<h1>Show Watcher</h1><p>District and PVR movie listing alerts, checked continuously while this server runs.</p>
<div class='notice'>{html.escape(notice)}</div><section class=card><h2>Add a trigger</h2>
<form class=grid method=post action='/add'><fieldset class=wide><label>Platforms</label><div class=platforms><label><input checked type=checkbox name=platform value=district> District</label><label><input type=checkbox name=platform value=pvr> PVR Cinemas</label></div></fieldset>
<label class=wide>District movie page URL<input type=url name=district_url placeholder='https://www.district.in/movies/...'></label><label class=wide>PVR movie page URL<input type=url name=pvr_url placeholder='https://www.pvrcinemas.com/moviesessions/...'></label>
<label>Experience filter<select name=experience><option value=ALL>All experiences</option><option value=IMAX>IMAX</option><option value=4DX>4DX</option></select></label><label>City<select id=city-choice name=city_choice onchange='updateCity()'><option value=bengaluru>Bengaluru</option><option value=delhi>Delhi</option><option value=other>Other</option></select></label>
<label class=wide id=city-custom>Custom city<input name=city_custom placeholder='Enter city name or city key'></label>
<label>Movie name<input required name=name placeholder='The Odyssey'></label><label>Start date<input required type=date name=start_date></label>
<label>End date<input required type=date name=end_date></label>
<label>Frequency (minutes)<input required type=number min=5 name=frequency_minutes value=120></label><div></div>
<label>Latitude<input required id=latitude type=number step=any name=latitude placeholder='Use current location or enter manually'></label><label>Longitude<input required id=longitude type=number step=any name=longitude placeholder='Use current location or enter manually'></label>
<div class=wide id=location-note>Requesting your current location to prefill coordinates…</div><div class=wide><button>Add trigger(s)</button></div></form></section><section class=card><h2>Active triggers</h2><table><thead><tr><th>Movie / schedule</th><th>City</th><th>Last check</th><th>Status</th><th></th></tr></thead><tbody>{rows}</tbody></table></section><script>function updateCity(){{const other=document.getElementById('city-choice').value==='other';const custom=document.getElementById('city-custom');custom.style.display=other?'grid':'none';custom.querySelector('input').required=other}}function setLocationNote(message){{document.getElementById('location-note').textContent=message}}if(navigator.geolocation){{navigator.geolocation.getCurrentPosition(position=>{{const latitude=document.getElementById('latitude'),longitude=document.getElementById('longitude');if(!latitude.value)latitude.value=position.coords.latitude.toFixed(6);if(!longitude.value)longitude.value=position.coords.longitude.toFixed(6);setLocationNote('Coordinates prefilled from your current location. You can edit them.')}},()=>setLocationNote('Location was not shared. Enter latitude and longitude manually.'),{{enableHighAccuracy:false,timeout:10000}})}}else{{setLocationNote('Location is not supported by this browser. Enter coordinates manually.')}}</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def authenticate(self) -> bool:
        password = os.getenv("APP_PASSWORD")
        if not password:
            return True
        expected = "Basic " + base64.b64encode(f"watcher:{password}".encode()).decode()
        if self.headers.get("Authorization") == expected:
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="Show Watcher"')
        self.end_headers()
        return False

    def do_GET(self) -> None:
        if not self.authenticate():
            return
        message = parse_qs(urlparse(self.path).query).get("message", [""])[0]
        body = page(triggers(), message).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        if not self.authenticate():
            return
        length = int(self.headers.get("Content-Length", "0"))
        submitted = parse_qs(self.rfile.read(length).decode())
        fields = {key: values[0].strip() for key, values in submitted.items()}
        try:
            with LOCK:
                items = triggers()
                if self.path == "/add":
                    frequency = int(fields["frequency_minutes"])
                    if frequency < 5:
                        raise ValueError("Frequency must be at least 5 minutes")
                    providers = submitted.get("platform", [])
                    if not providers or any(provider not in {"district", "pvr"} for provider in providers):
                        raise ValueError("Select District, PVR Cinemas, or both")
                    city = fields.get("city_custom", "") if fields.get("city_choice") == "other" else fields.get("city_choice", "")
                    if not city:
                        raise ValueError("Select a city or enter a custom city")
                    target_dates = trigger_dates(fields["start_date"], fields["end_date"])
                    added = 0
                    for provider in providers:
                        source_url = fields.get(f"{provider}_url", "")
                        if not source_url:
                            raise ValueError(f"Provide the {provider.title()} movie page URL")
                        for target_date in target_dates:
                            items.append({"id": str(uuid.uuid4()), "provider": provider, "name": fields["name"], "source_url": source_url, "city_key": city.lower(), "city_name": city, "date": target_date, "experience": fields.get("experience", "ALL").upper(), "latitude": float(fields["latitude"]), "longitude": float(fields["longitude"]), "frequency_minutes": frequency, "last_checked_at": None, "last_error": None})
                            added += 1
                    message = f"{added} trigger{'s' if added != 1 else ''} added for {len(target_dates)} date{'s' if len(target_dates) != 1 else ''}. They will be checked within 15 seconds."
                elif self.path == "/delete":
                    items = [item for item in items if item["id"] != fields.get("id")]
                    message = "Trigger deleted."
                else:
                    raise ValueError("Unknown action")
                save_triggers(items)
        except (KeyError, ValueError) as error:
            message = f"Could not save trigger: {error}"
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", f"/?message={message.replace(' ', '+')}")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")


def main() -> None:
    bootstrap()
    threading.Thread(target=scheduler, daemon=True).start()
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"Show Watcher running at http://localhost:{port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
