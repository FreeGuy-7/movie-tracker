#!/usr/bin/env python3
"""Multi-user web dashboard and continuous movie-listing scheduler."""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, urlparse

from app import (
    IST,
    Listing,
    api_parameters,
    debug_log,
    discord_mention,
    fetch_listing,
    format_date,
    format_listing_report,
    format_showtime,
    movie_url,
    pvr_parameters,
    send_discord_text,
    summarize,
)
from database import DocumentStore


DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DB_PATH = Path(os.getenv("DB_PATH", str(DATA_DIR / "database")))
STORE = DocumentStore(DB_PATH)
DB_LOCK = threading.RLock()
MAX_ACTIVE_TRIGGERS = 5
SESSION_DAYS = 30
CITY_COORDINATES = {
    "bengaluru": ("12.963622", "77.646624"),
    "delhi": ("28.513357", "77.004640"),
}
LAST_HEARTBEAT = 0.0


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 310000)
    return f"pbkdf2_sha256$310000${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def password_matches(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, encoded_salt, encoded_digest = stored.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.urlsafe_b64decode(encoded_salt.encode())
        expected = base64.urlsafe_b64decode(encoded_digest.encode())
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iterations))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def user_by_username(username: str) -> dict | None:
    normalized = username.casefold()
    return next((user for user in STORE.all("users") if user.get("username", "").casefold() == normalized), None)


def ensure_admin() -> None:
    admin_username = os.getenv("ADMIN_USERNAME", "admin").strip() or "admin"
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    existing = next((user for user in STORE.all("users") if user.get("role") == "admin"), None)
    if existing or not admin_password:
        return
    STORE.put("users", {"id": str(uuid.uuid4()), "username": admin_username, "password_hash": password_hash(admin_password), "role": "admin", "discord_user_id": os.getenv("ADMIN_DISCORD_USER_ID", "").strip(), "created_at": now()})


def migrate_legacy_data() -> None:
    if STORE.count("triggers") or not ensure_legacy_admin():
        return
    legacy_path = DATA_DIR / "triggers.json"
    if not legacy_path.exists():
        return
    try:
        legacy_triggers = json.loads(legacy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    admin = next((user for user in STORE.all("users") if user.get("role") == "admin"), None)
    if not admin:
        return
    for trigger in legacy_triggers if isinstance(legacy_triggers, list) else []:
        trigger = {**trigger, "id": str(trigger.get("id") or uuid.uuid4()), "owner_id": admin["id"]}
        STORE.put("triggers", trigger)
    legacy_state_path = DATA_DIR / "state.json"
    if legacy_state_path.exists():
        try:
            legacy_states = json.loads(legacy_state_path.read_text(encoding="utf-8"))
            for trigger_id, state in legacy_states.items():
                STORE.put("states", {"id": trigger_id, **state})
        except (OSError, json.JSONDecodeError):
            pass


def ensure_legacy_admin() -> bool:
    ensure_admin()
    return any(user.get("role") == "admin" for user in STORE.all("users"))


def trigger_expired(trigger: dict, current_date: date | None = None) -> bool:
    try:
        target_date = date.fromisoformat(str(trigger["date"]))
    except (KeyError, TypeError, ValueError):
        return False
    return target_date < (current_date or datetime.now(IST).date())


def cleanup_expired() -> list[dict]:
    removed = 0
    active: list[dict] = []
    with DB_LOCK:
        for trigger in STORE.all("triggers"):
            if trigger_expired(trigger):
                STORE.delete("triggers", trigger["id"])
                STORE.delete("states", trigger["id"])
                removed += 1
            else:
                active.append(trigger)
    if removed:
        debug_log("expired_triggers_removed", removed=removed)
    return active


def trigger_dates(start_value: str, end_value: str) -> list[str]:
    start = date.fromisoformat(start_value)
    end = date.fromisoformat(end_value)
    if end < start:
        raise ValueError("End date must be on or after the start date")
    if (end - start).days > 366:
        raise ValueError("Date range cannot exceed 367 days")
    return [(start + timedelta(days=offset)).isoformat() for offset in range((end - start).days + 1)]


def listing_items(listings: list[Listing]) -> list[tuple[str, str, str]]:
    return sorted((listing.screen_format, listing.venue, showtime) for listing in listings for showtime in listing.showtimes)


def listing_signature(listings: list[Listing]) -> str:
    return hashlib.sha256(json.dumps(listing_items(listings)).encode()).hexdigest()


def trigger_call_key(trigger: dict) -> str:
    provider = trigger.get("provider", "district")
    parameters = api_parameters(trigger) if provider == "district" else pvr_parameters(trigger)
    return json.dumps({"provider": provider, "source_url": trigger.get("source_url", ""), "parameters": parameters}, sort_keys=True, separators=(",", ":"))


def due(trigger: dict, timestamp: float) -> bool:
    previous = trigger.get("last_checked_at")
    if not previous:
        return True
    try:
        return timestamp - datetime.fromisoformat(previous).timestamp() >= int(trigger["frequency_minutes"]) * 60
    except (KeyError, TypeError, ValueError):
        return True


def format_new_show_alert(trigger: dict, new_items: list[tuple[str, str, str]]) -> str:
    service = "District" if trigger.get("provider", "district") == "district" else "PVR Cinemas"
    grouped: dict[str, dict[str, list[str]]] = {}
    for screen_format, venue, showtime in new_items:
        grouped.setdefault(screen_format, {}).setdefault(venue, []).append(showtime)
    lines = [
        "🚨 **New showtimes added**",
        f"🎬 **{trigger['name']}**",
        f"🏢 **Service:** {service}",
        f"📅 **Date:** {format_date(trigger['date'])} · 📍 **City:** {trigger.get('city_name', trigger.get('city_key', '')).title()}",
        f"🎟️ **New shows:** {len(new_items)}",
    ]
    for screen_format in sorted(grouped):
        lines.append(f"\n━━ **{screen_format.upper()}** ━━")
        for venue in sorted(grouped[screen_format]):
            times = " · ".join(format_showtime(showtime) for showtime in sorted(grouped[screen_format][venue]))
            lines.append(f"**{venue}**\n↳ {times}")
    lines.append(f"\n🔗 **Booking link:** {movie_url(trigger)}")
    return "\n".join(lines)


def user_mentions(users: list[dict]) -> str:
    mentions = sorted({f"<@{user['discord_user_id']}>" for user in users if str(user.get("discord_user_id", "")).isdigit()})
    return " ".join(mentions) or discord_mention()


def process_group(group: list[dict], timestamp: float) -> None:
    due_triggers = [trigger for trigger in group if due(trigger, timestamp)]
    if not due_triggers:
        return
    representative = due_triggers[0]
    try:
        payload = fetch_listing(representative)
        listings = summarize(payload, representative)
        current_items = listing_items(listings)
        signature = listing_signature(listings)
        new_alert_items: set[tuple[str, str, str]] = set()
        alert_users: dict[str, dict] = {}
        for trigger in due_triggers:
            previous = STORE.get("states", trigger["id"]) or {}
            previous_items = {tuple(item) for item in previous.get("shows", [])}
            new_items = [item for item in current_items if item not in previous_items]
            if previous and new_items:
                new_alert_items.update(new_items)
                owner = STORE.get("users", trigger["owner_id"])
                if owner:
                    alert_users[owner["id"]] = owner
            trigger["last_checked_at"] = now()
            trigger["last_error"] = None
            STORE.put("triggers", trigger)
            STORE.put("states", {"id": trigger["id"], "signature": signature, "shows": current_items, "checked_at": trigger["last_checked_at"]})
            run_webhook = os.getenv("DISCORD_TRIGGER_WEBHOOK_URL")
            if run_webhook:
                send_discord_text(run_webhook, format_listing_report(trigger, listings))
        if new_alert_items and os.getenv("DISCORD_NEW_SHOW_WEBHOOK_URL"):
            send_discord_text(os.environ["DISCORD_NEW_SHOW_WEBHOOK_URL"], format_new_show_alert(representative, sorted(new_alert_items)), user_mentions(list(alert_users.values())))
        debug_log("api_call_success", provider=representative.get("provider"), trigger_count=len(due_triggers), listing_groups=len(listings), showtimes=len(current_items))
    except Exception as error:
        for trigger in due_triggers:
            trigger["last_checked_at"] = now()
            trigger["last_error"] = str(error)
            STORE.put("triggers", trigger)
        debug_log("api_call_error", provider=representative.get("provider"), trigger_count=len(due_triggers), error=str(error))


def run_due() -> None:
    if not DB_LOCK.acquire(blocking=False):
        return
    try:
        active = cleanup_expired()
        groups: dict[str, list[dict]] = {}
        for trigger in active:
            try:
                groups.setdefault(trigger_call_key(trigger), []).append(trigger)
            except (KeyError, TypeError, ValueError) as error:
                trigger["last_error"] = str(error)
                STORE.put("triggers", trigger)
        timestamp = time.time()
        for group in groups.values():
            process_group(group, timestamp)
    finally:
        DB_LOCK.release()


def send_heartbeat() -> None:
    global LAST_HEARTBEAT
    interval = max(1, int(os.getenv("HEARTBEAT_MINUTES", "60"))) * 60
    if time.time() - LAST_HEARTBEAT < interval or not os.getenv("DISCORD_STATUS_WEBHOOK_URL"):
        return
    active = cleanup_expired()
    errors = sum(bool(trigger.get("last_error")) for trigger in active)
    try:
        send_discord_text(os.environ["DISCORD_STATUS_WEBHOOK_URL"], f"✅ **Show Watcher is running**\nActive triggers: {len(active)}\nTriggers with errors: {errors}\nUTC: {now()}")
        LAST_HEARTBEAT = time.time()
    except Exception as error:
        debug_log("heartbeat_error", error=str(error))


def scheduler() -> None:
    debug_log("scheduler_started", database=str(DB_PATH))
    while True:
        run_due()
        send_heartbeat()
        time.sleep(15)


def session_cookie(token: str, max_age: int = SESSION_DAYS * 86400) -> str:
    secure = "; Secure" if os.getenv("COOKIE_SECURE", "0") == "1" else ""
    return f"show_watcher_session={token}; Max-Age={max_age}; Path=/; HttpOnly; SameSite=Lax{secure}"


def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    token_id = hashlib.sha256(token.encode()).hexdigest()
    STORE.put("sessions", {"id": token_id, "user_id": user_id, "expires_at": (datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)).isoformat()})
    return token


def current_user(handler: BaseHTTPRequestHandler) -> dict | None:
    cookies = SimpleCookie(handler.headers.get("Cookie", ""))
    token = cookies.get("show_watcher_session")
    if not token:
        return None
    token_id = hashlib.sha256(token.value.encode()).hexdigest()
    session = STORE.get("sessions", token_id)
    if not session:
        return None
    try:
        if datetime.fromisoformat(session["expires_at"]) <= datetime.now(timezone.utc):
            STORE.delete("sessions", token_id)
            return None
    except (KeyError, ValueError):
        return None
    return STORE.get("users", session["user_id"])


def render_shell(title: str, body: str) -> bytes:
    return f"""<!doctype html><html><head><meta name=viewport content='width=device-width,initial-scale=1'><title>{escape(title)}</title><style>body{{max-width:1100px;margin:0 auto;padding:32px 18px 48px;font:15px system-ui;color:#172033;background:#f6f8fb}}h1{{margin:0 0 6px}}h2{{margin-top:0}}.card{{background:#fff;border:1px solid #e4e7ef;border-radius:14px;padding:22px;margin:20px 0;box-shadow:0 2px 8px #00000008}}form.grid{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}}label{{display:grid;gap:6px;font-size:13px;font-weight:650}}input,select{{width:100%;box-sizing:border-box;padding:10px;border:1px solid #b9c0cf;border-radius:8px;font:inherit}}.wide{{grid-column:1/-1}}button{{background:#3b5bdb;color:#fff;border:0;border-radius:8px;padding:10px 14px;font-weight:700;cursor:pointer}}button.quiet{{background:#fff;color:#b42318;border:1px solid #f3c7c4;padding:7px 10px}}a{{color:#3154c5}}.topbar{{display:flex;justify-content:space-between;gap:12px;align-items:center}}.notice{{color:#067647;font-weight:650;margin:10px 0}}.error{{color:#b42318;font-weight:650}}.muted,small{{color:#667085}}.platforms{{display:flex;gap:18px}}.platforms label{{display:flex;align-items:center;gap:7px;font-size:15px}}.platforms input{{width:auto}}.url-field{{display:none}}.edit-form{{display:flex;gap:5px;margin-bottom:5px}}.edit-form input{{width:90px;padding:7px}}table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:10px 7px;border-bottom:1px solid #e8eaf0;vertical-align:top}}details{{margin-top:8px}}@media(max-width:700px){{form.grid{{grid-template-columns:1fr}}.wide{{grid-column:auto}}.topbar{{align-items:flex-start;flex-direction:column}}}}</style></head><body>{body}</body></html>""".encode("utf-8")


def render_auth_page(signup: bool, error: str = "") -> bytes:
    if signup:
        body = f"<section class=card><h1>Create account</h1><p class=muted>Use the access key shared by the administrator.</p>{f'<p class=error>{escape(error)}</p>' if error else ''}<form method=post action='/signup' class=grid><label>Username<input required name=username autocomplete=username></label><label>Discord user ID<input required name=discord_user_id placeholder='Numeric ID for new-show tags'></label><label>Password<input required type=password name=password autocomplete=new-password></label><label>Confirm password<input required type=password name=confirm_password autocomplete=new-password></label><label class=wide>Access key<input required type=password name=access_key></label><div><button>Sign up</button> <a href='/login'>Back to login</a></div></form></section>"
    else:
        body = f"<section class=card><h1>Show Watcher</h1><p class=muted>Sign in to manage your movie triggers.</p>{f'<p class=error>{escape(error)}</p>' if error else ''}<form method=post action='/login'><label>Username<input required name=username autocomplete=username></label><label>Password<input required type=password name=password autocomplete=current-password></label><p><button>Log in</button> <a href='/signup'>Create account</a></p></form></section>"
    return render_shell("Sign up" if signup else "Log in", body)


def render_dashboard(user: dict, active: list[dict], all_users: list[dict], notice: str = "") -> bytes:
    visible = active if user.get("role") == "admin" else [trigger for trigger in active if trigger.get("owner_id") == user["id"]]
    owner_map = {item["id"]: item for item in all_users}
    rows = []
    for trigger in visible:
        owner = owner_map.get(trigger.get("owner_id"), {})
        admin_form = ""
        if user.get("role") == "admin":
            options = "".join(f"<option value='{escape(item['id'])}' {'selected' if item['id'] == trigger.get('owner_id') else ''}>{escape(item['username'])}</option>" for item in all_users)
            admin_form = f"<details><summary>Edit all parameters</summary><form method=post action='/admin/edit' class=grid><input type=hidden name=id value='{escape(trigger['id'])}'><label>Owner<select name=owner_id>{options}</select></label><label>Provider<select name=provider><option {'selected' if trigger.get('provider') == 'district' else ''}>district</option><option {'selected' if trigger.get('provider') == 'pvr' else ''}>pvr</option></select></label><label>Name<input required name=name value='{escape(trigger.get('name', ''))}'></label><label>Movie URL<input required type=url name=source_url value='{escape(trigger.get('source_url', ''))}'></label><label>City<input required name=city_name value='{escape(trigger.get('city_name', trigger.get('city_key', '')))}'></label><label>Date<input required type=date name=date value='{escape(trigger.get('date', ''))}'></label><label>Experience<select name=experience><option {'selected' if trigger.get('experience') == 'ALL' else ''}>ALL</option><option {'selected' if trigger.get('experience') == 'IMAX' else ''}>IMAX</option><option {'selected' if trigger.get('experience') == '4DX' else ''}>4DX</option></select></label><label>Frequency<input required min=5 type=number name=frequency_minutes value='{escape(trigger.get('frequency_minutes', 30))}'></label><label>Latitude<input required type=number step=any name=latitude value='{escape(trigger.get('latitude', ''))}'></label><label>Longitude<input required type=number step=any name=longitude value='{escape(trigger.get('longitude', ''))}'></label><div><button>Save all</button></div></form></details>"
        rows.append(f"<tr><td><strong>{escape(trigger.get('name', ''))}</strong><br><small>{escape(trigger.get('provider', '').upper())} · {escape(trigger.get('experience', 'ALL'))} · {escape(trigger.get('date', ''))}</small></td><td>{escape(trigger.get('city_name', trigger.get('city_key', '')).title())}<br><small>{escape(owner.get('username', '')) if user.get('role') == 'admin' else ''}</small></td><td>{escape(trigger.get('last_checked_at') or 'Not checked')}</td><td>{escape(trigger.get('last_error') or 'Healthy')}</td><td><form class=edit-form method=post action='/edit'><input type=hidden name=id value='{escape(trigger['id'])}'><input required min=5 type=number name=frequency_minutes value='{escape(trigger.get('frequency_minutes', 30))}' aria-label='Frequency in minutes'><button class=quiet>Save</button></form><form method=post action='/delete'><input type=hidden name=id value='{escape(trigger['id'])}'><button class=quiet>Delete</button></form>{admin_form}</td></tr>")
    rows_html = "".join(rows) or "<tr><td colspan=5>No triggers yet.</td></tr>"
    users_html = ""
    if user.get("role") == "admin":
        user_rows = "".join(f"<tr><td>{escape(item['username'])}</td><td>{escape(item.get('role', 'user'))}</td><td>{escape(item.get('discord_user_id', '') or 'Not set')}</td><td>{sum(1 for trigger in active if trigger.get('owner_id') == item['id'])}/{'Unlimited' if item.get('role') == 'admin' else MAX_ACTIVE_TRIGGERS}</td></tr>" for item in all_users)
        users_html = f"<section class=card><h2>Users</h2><table><tr><th>Username</th><th>Role</th><th>Discord ID</th><th>Active triggers</th></tr>{user_rows}</table></section>"
    body = f"<div class=topbar><div><h1>Show Watcher</h1><p class=muted>Signed in as <strong>{escape(user['username'])}</strong> ({escape(user['role'])})</p></div><a href='/logout'>Log out</a></div><p class=notice>{escape(notice)}</p><section class=card><h2>Add triggers</h2><p class=muted>{'Admin view: all active triggers are visible.' if user.get('role') == 'admin' else f'You can have up to {MAX_ACTIVE_TRIGGERS} active triggers.'}</p><form method=post action='/add' class=grid><fieldset class=wide><label>Platforms</label><div class=platforms><label><input checked type=checkbox name=platform value=district onchange='updatePlatforms()'> District</label><label><input type=checkbox name=platform value=pvr onchange='updatePlatforms()'> PVR Cinemas</label></div></fieldset><label class='wide url-field' id=district-url-field>District movie URL<input type=url name=district_url placeholder='https://www.district.in/movies/...'></label><label class='wide url-field' id=pvr-url-field>PVR movie URL<input type=url name=pvr_url placeholder='https://www.pvrcinemas.com/moviesessions/...'></label><label>Movie name<input required name=name placeholder='The Odyssey'></label><label>City<select name=city_choice id=city-choice onchange=updateCity()><option value=bengaluru>Bengaluru</option><option value=delhi>Delhi</option><option value=other>Other</option></select></label><label class=wide id=city-custom>Custom city<input name=city_custom></label><label>Experience<select name=experience><option>ALL</option><option>IMAX</option><option>4DX</option></select></label><label>Frequency (minutes)<input required min=5 type=number name=frequency_minutes value=30></label><label>Start date<input required type=date name=start_date></label><label>End date <small>(optional)</small><input type=date name=end_date></label><label>Latitude<input required id=latitude type=number step=any name=latitude></label><label>Longitude<input required id=longitude type=number step=any name=longitude></label><div class=wide><button>Add trigger(s)</button> <span class=muted id=location-note>City defaults are applied automatically.</span></div></form></section><section class=card><h2>{'All active triggers' if user.get('role') == 'admin' else 'My active triggers'}</h2><table><tr><th>Movie</th><th>City / owner</th><th>Last check</th><th>Status</th><th>Actions</th></tr>{rows_html}</table></section>{users_html}<script>const coords={json.dumps(CITY_COORDINATES)};function updateCity(){{const choice=document.getElementById('city-choice').value;const custom=document.getElementById('city-custom');custom.style.display=choice==='other'?'grid':'none';custom.querySelector('input').required=choice==='other';const point=coords[choice];if(point){{latitude.value=point[0];longitude.value=point[1];locationNote.textContent='Using '+choice+' default coordinates.'}}else{{latitude.value='';longitude.value='';locationNote.textContent='Enter custom city coordinates.'}}}}function updatePlatforms(){{for(const provider of ['district','pvr']){{const check=document.querySelector('input[name=platform][value='+provider+']');const field=document.getElementById(provider+'-url-field');field.style.display=check.checked?'grid':'none';field.querySelector('input').required=check.checked}}}}const latitude=document.getElementById('latitude'),longitude=document.getElementById('longitude'),locationNote=document.getElementById('location-note');updateCity();updatePlatforms();</script>"
    return render_shell("Dashboard", body)


class Handler(BaseHTTPRequestHandler):
    def send_html(self, body: bytes, status: int = HTTPStatus.OK, cookie: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, location: str, cookie: str | None = None) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def form_data(self) -> dict[str, str | list[str]]:
        length = int(self.headers.get("Content-Length", "0"))
        values = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        return {key: value if len(value) > 1 else value[0] for key, value in values.items()}

    @staticmethod
    def value(data: dict[str, str | list[str]], key: str, default: str = "") -> str:
        value = data.get(key, default)
        return value[-1] if isinstance(value, list) else value

    def require_user(self) -> dict | None:
        user = current_user(self)
        if not user:
            self.redirect("/login")
        return user

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/login":
            self.send_html(render_auth_page(False))
            return
        if path == "/signup":
            self.send_html(render_auth_page(True))
            return
        if path == "/logout":
            self.redirect("/login", session_cookie("", 0))
            return
        user = self.require_user()
        if not user:
            return
        if path == "/":
            notice = parse_qs(urlparse(self.path).query).get("notice", [""])[0]
            with DB_LOCK:
                active = cleanup_expired()
                users = STORE.all("users")
            self.send_html(render_dashboard(user, active, users, notice))
            return
        self.send_html(render_shell("Not found", "<section class=card><h1>Not found</h1><a href='/'>Back to dashboard</a></section>"), HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        data = self.form_data()
        if path == "/login":
            username = self.value(data, "username").strip()
            user = user_by_username(username)
            if not user or not password_matches(self.value(data, "password"), user.get("password_hash", "")):
                self.send_html(render_auth_page(False, "Invalid username or password."), HTTPStatus.UNAUTHORIZED)
                return
            self.redirect("/", session_cookie(create_session(user["id"])))
            return
        if path == "/signup":
            username = self.value(data, "username").strip()
            password = self.value(data, "password")
            error = ""
            if not os.getenv("SIGNUP_ACCESS_KEY") or not hmac.compare_digest(self.value(data, "access_key"), os.getenv("SIGNUP_ACCESS_KEY", "")):
                error = "Invalid access key."
            elif not re.fullmatch(r"[A-Za-z0-9_.-]{3,32}", username):
                error = "Username must be 3–32 letters, numbers, dots, dashes, or underscores."
            elif user_by_username(username):
                error = "That username is already in use."
            elif len(password) < 8 or password != self.value(data, "confirm_password"):
                error = "Passwords must match and be at least 8 characters."
            elif not self.value(data, "discord_user_id").isdigit():
                error = "Enter a numeric Discord user ID."
            if error:
                self.send_html(render_auth_page(True, error), HTTPStatus.BAD_REQUEST)
                return
            user = {"id": str(uuid.uuid4()), "username": username, "password_hash": password_hash(password), "role": "user", "discord_user_id": self.value(data, "discord_user_id"), "created_at": now()}
            with DB_LOCK:
                STORE.put("users", user)
            self.redirect("/", session_cookie(create_session(user["id"])))
            return

        user = self.require_user()
        if not user:
            return
        try:
            with DB_LOCK:
                cleanup_expired()
                if path == "/add":
                    self.add_triggers(user, data)
                elif path == "/edit":
                    self.edit_frequency(user, data)
                elif path == "/delete":
                    self.delete_trigger(user, data)
                elif path == "/admin/edit":
                    if user.get("role") != "admin":
                        raise ValueError("Admin access required.")
                    self.admin_edit(data)
                else:
                    self.send_html(render_shell("Not found", "<section class=card><h1>Not found</h1></section>"), HTTPStatus.NOT_FOUND)
                    return
            self.redirect("/?notice=" + quote_plus("Saved successfully."))
        except (ValueError, KeyError, TypeError) as error:
            with DB_LOCK:
                active = cleanup_expired()
                users = STORE.all("users")
            self.send_html(render_dashboard(user, active, users, str(error)), HTTPStatus.BAD_REQUEST)

    def add_triggers(self, user: dict, data: dict[str, str | list[str]]) -> None:
        platforms = data.get("platform", [])
        platforms = platforms if isinstance(platforms, list) else [platforms]
        platforms = [provider for provider in platforms if provider in {"district", "pvr"}]
        if not platforms:
            raise ValueError("Select at least one platform.")
        city_choice = self.value(data, "city_choice")
        city_name = self.value(data, "city_custom").strip() if city_choice == "other" else city_choice
        if not city_name:
            raise ValueError("City is required.")
        dates = trigger_dates(self.value(data, "start_date"), self.value(data, "end_date") or self.value(data, "start_date"))
        frequency = int(self.value(data, "frequency_minutes"))
        if frequency < 5:
            raise ValueError("Frequency must be at least 5 minutes.")
        latitude = float(self.value(data, "latitude"))
        longitude = float(self.value(data, "longitude"))
        existing_count = len(STORE.find("triggers", lambda item: item.get("owner_id") == user["id"] and not trigger_expired(item)))
        if user.get("role") != "admin" and existing_count + len(platforms) * len(dates) > MAX_ACTIVE_TRIGGERS:
            raise ValueError(f"Each user can have at most {MAX_ACTIVE_TRIGGERS} active triggers.")
        for provider in platforms:
            source_url = self.value(data, f"{provider}_url").strip()
            if not source_url:
                raise ValueError(f"A {provider.title()} URL is required.")
            for target_date in dates:
                trigger = {"id": str(uuid.uuid4()), "owner_id": user["id"], "provider": provider, "name": self.value(data, "name").strip(), "source_url": source_url, "city_key": city_name.casefold(), "city_name": city_name, "date": target_date, "experience": self.value(data, "experience", "ALL").upper(), "frequency_minutes": frequency, "latitude": latitude, "longitude": longitude, "last_checked_at": None, "last_error": None, "created_at": now()}
                STORE.put("triggers", trigger)

    def owned_trigger(self, user: dict, trigger_id: str) -> dict:
        trigger = STORE.get("triggers", trigger_id)
        if not trigger or (user.get("role") != "admin" and trigger.get("owner_id") != user["id"]):
            raise ValueError("Trigger not found.")
        return trigger

    def edit_frequency(self, user: dict, data: dict[str, str | list[str]]) -> None:
        trigger = self.owned_trigger(user, self.value(data, "id"))
        frequency = int(self.value(data, "frequency_minutes"))
        if frequency < 5:
            raise ValueError("Frequency must be at least 5 minutes.")
        trigger["frequency_minutes"] = frequency
        STORE.put("triggers", trigger)

    def delete_trigger(self, user: dict, data: dict[str, str | list[str]]) -> None:
        trigger = self.owned_trigger(user, self.value(data, "id"))
        STORE.delete("triggers", trigger["id"])
        STORE.delete("states", trigger["id"])

    def admin_edit(self, data: dict[str, str | list[str]]) -> None:
        trigger = STORE.get("triggers", self.value(data, "id"))
        owner = STORE.get("users", self.value(data, "owner_id"))
        if not trigger or not owner:
            raise ValueError("Trigger or owner not found.")
        provider = self.value(data, "provider")
        if provider not in {"district", "pvr"}:
            raise ValueError("Invalid provider.")
        frequency = int(self.value(data, "frequency_minutes"))
        if frequency < 5:
            raise ValueError("Frequency must be at least 5 minutes.")
        owner_count = len(STORE.find("triggers", lambda item: item.get("owner_id") == owner["id"] and item["id"] != trigger["id"] and not trigger_expired(item)))
        if owner.get("role") != "admin" and owner_count >= MAX_ACTIVE_TRIGGERS and not trigger_expired(trigger):
            raise ValueError(f"That user already has {MAX_ACTIVE_TRIGGERS} active triggers.")
        trigger.update({"owner_id": owner["id"], "provider": provider, "name": self.value(data, "name").strip(), "source_url": self.value(data, "source_url").strip(), "city_key": self.value(data, "city_name").strip().casefold(), "city_name": self.value(data, "city_name").strip(), "date": self.value(data, "date"), "experience": self.value(data, "experience", "ALL").upper(), "frequency_minutes": frequency, "latitude": float(self.value(data, "latitude")), "longitude": float(self.value(data, "longitude")), "last_error": None})
        STORE.put("triggers", trigger)

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> None:
    ensure_admin()
    migrate_legacy_data()
    threading.Thread(target=scheduler, daemon=True).start()
    port = int(os.getenv("PORT", "8080"))
    server = ThreadingHTTPServer((os.getenv("HOST", "0.0.0.0"), port), Handler)
    print(f"Show Watcher listening on http://127.0.0.1:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        STORE.close()


if __name__ == "__main__":
    main()
