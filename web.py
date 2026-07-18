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
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app import Listing, fetch_listing, format_listing_report, send_discord_text, summarize


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
    return load(TRIGGERS_PATH, [])  # type: ignore[return-value]


def save_triggers(items: list[dict]) -> None:
    save(TRIGGERS_PATH, items)


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
        listings = summarize(fetch_listing(trigger))
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
            send_discord_text(new_show_webhook, f"🚨 **New showtime{'s' if len(new_items) > 1 else ''} added for {trigger['name']}**\n{examples}{suffix}", os.getenv("DISCORD_MENTION", "@here"))
        state[trigger["id"]] = {"signature": signature, "shows": current_items, "checked_at": now()}
        trigger["last_error"] = None
    except Exception as error:
        trigger["last_error"] = str(error)
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
    while True:
        run_due()
        send_heartbeat()
        time.sleep(15)


def page(items: list[dict], notice: str = "") -> str:
    rows = "".join(
        f"<tr><td><strong>{html.escape(item['name'])}</strong><br><small>{html.escape(item['date'])} · every {item['frequency_minutes']} min</small></td>"
        f"<td>{html.escape(item['city_key'].title())}</td><td>{html.escape(item.get('last_checked_at') or 'Not checked')}</td>"
        f"<td>{html.escape(item.get('last_error') or 'Healthy')}</td><td><form method='post' action='/delete'><input type='hidden' name='id' value='{item['id']}'><button class='quiet'>Delete</button></form></td></tr>"
        for item in items
    ) or "<tr><td colspan='5'>No triggers yet.</td></tr>"
    return f"""<!doctype html><html><head><meta name=viewport content='width=device-width,initial-scale=1'>
<title>Show Watcher</title><style>
body{{max-width:960px;margin:40px auto;padding:0 18px;font:16px system-ui;color:#172033;background:#f7f8fc}}h1{{margin-bottom:4px}}.card{{background:white;border:1px solid #e4e7ef;border-radius:12px;padding:22px;margin:22px 0;box-shadow:0 1px 3px #00000008}}form.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px}}label{{font-size:13px;font-weight:650;display:grid;gap:5px}}input{{padding:10px;border:1px solid #b9c0cf;border-radius:7px;font:inherit}}.wide{{grid-column:1/-1}}button{{background:#3b5bdb;color:white;border:0;border-radius:7px;padding:10px 14px;font-weight:650;cursor:pointer}}button.quiet{{background:#fff;color:#b42318;border:1px solid #f3c7c4;padding:7px 10px}}table{{width:100%;border-collapse:collapse}}td,th{{text-align:left;padding:12px 8px;border-bottom:1px solid #e8eaf0;vertical-align:top}}small{{color:#667085}}.notice{{color:#067647;font-weight:650}}</style></head><body>
<h1>Show Watcher</h1><p>District movie listing alerts, checked continuously while this server runs.</p>
<div class='notice'>{html.escape(notice)}</div><section class=card><h2>Add a trigger</h2>
<form class=grid method=post action='/add'><label class=wide>District movie URL<input required type=url name=district_url placeholder='https://www.district.in/movies/...'></label>
<label>Movie name<input required name=name placeholder='The Odyssey'></label><label>Target date<input required type=date name=date></label>
<label>City key<input required name=city_key placeholder='bengaluru'></label><label>Frequency (minutes)<input required type=number min=5 name=frequency_minutes value=120></label>
<label>Latitude<input required type=number step=any name=latitude placeholder='12.9636'></label><label>Longitude<input required type=number step=any name=longitude placeholder='77.6469'></label>
<div class=wide><button>Add trigger</button></div></form></section><section class=card><h2>Active triggers</h2><table><thead><tr><th>Movie / schedule</th><th>City</th><th>Last check</th><th>Status</th><th></th></tr></thead><tbody>{rows}</tbody></table></section></body></html>"""


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
        fields = {key: values[0].strip() for key, values in parse_qs(self.rfile.read(length).decode()).items()}
        try:
            with LOCK:
                items = triggers()
                if self.path == "/add":
                    frequency = int(fields["frequency_minutes"])
                    if frequency < 5:
                        raise ValueError("Frequency must be at least 5 minutes")
                    items.append({"id": str(uuid.uuid4()), "name": fields["name"], "district_url": fields["district_url"], "city_key": fields["city_key"].lower(), "date": fields["date"], "latitude": float(fields["latitude"]), "longitude": float(fields["longitude"]), "frequency_minutes": frequency, "last_checked_at": None, "last_error": None})
                    message = "Trigger added. It will be checked within 15 seconds."
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
