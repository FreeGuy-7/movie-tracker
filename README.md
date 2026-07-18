# Show Watcher

Lightweight Python 3.11 app for monitoring District and PVR Cinemas movie listings and sending Discord notifications.

## Local setup

```sh
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
# Set ADMIN_PASSWORD and SIGNUP_ACCESS_KEY in .env.
python web.py
```

Open `http://localhost:8080`. The first startup creates the admin account from `ADMIN_USERNAME` and `ADMIN_PASSWORD`. Users sign up with the shared `SIGNUP_ACCESS_KEY` and provide their numeric Discord user ID for new-show mentions.

The app stores users, sessions, triggers, and listing state in an embedded LMDB database at `DB_PATH` (default `data/database`). It does not use JSON files for active data. The `data/` directory is ignored by Git and must be persisted in deployment. Existing `data/triggers.json` and `data/state.json` files are imported once into the initial admin account when the database is empty.

## Dashboard behavior

- Users can add District, PVR, or both platforms in one form.
- A date range creates one trigger per provider and date. The end date is optional.
- Two different URLs for the same movie/date remain separate triggers; identical provider/API requests share one backend call.
- Regular users can have at most five active triggers; admins are not limited. Expired dates are removed automatically.
- Users can edit frequency and delete their own triggers.
- Admins can see every trigger and user, and edit every trigger parameter and owner.
- District and PVR support `ALL`, `IMAX`, and `4DX` experience filters where the provider supports them.

Use public movie page URLs, not copied API requests or browser cookies. Example URLs:

```text
https://www.district.in/movies/the-odyssey-movie-tickets-in-bengaluru-MV187151?frmtid=...
https://www.pvrcinemas.com/moviesessions/Bengaluru/THE-ODYSSEY/35098
```

## Discord

Set three separate webhook URLs:

```sh
DISCORD_STATUS_WEBHOOK_URL=  # service heartbeat
DISCORD_TRIGGER_WEBHOOK_URL= # report from each trigger run
DISCORD_NEW_SHOW_WEBHOOK_URL= # new show alerts and user mentions
```

New-show alerts group sessions by format and cinema, include IST showtimes and the booking link, and combine all affected users into one Discord message. Discord webhooks require numeric user IDs for tags; usernames and display names cannot be resolved automatically. Set `HEARTBEAT_MINUTES=1` for testing, then use a larger value in deployment. Leave `DEBUG_LOG_PATH` unset in deployed environments; local diagnostics can use `DEBUG_LOG_PATH=debug.log`.

## Configuration

Important environment variables:

```sh
ADMIN_USERNAME=admin
ADMIN_PASSWORD=choose-a-strong-password
SIGNUP_ACCESS_KEY=long-random-invite-key
DB_PATH=data/database
DATA_DIR=data
PORT=8080
COOKIE_SECURE=0 # set 1 behind HTTPS
```

Keep `.env` or the deployment environment file outside Git. Never put Discord webhook URLs, passwords, access keys, cookies, guest tokens, or request IDs in source control.

## Deployment

An always-on small VM is the simplest low-cost option. For a one-month test, a small paid VPS is generally easier than relying on Oracle free-tier capacity. A home machine or Raspberry Pi is free if it is already running.

### systemd on Ubuntu

```sh
cd /opt
git clone git@github.com:FreeGuy-7/movie-tracker.git
cd movie-tracker
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example /etc/show-watcher.env
sudo chmod 600 /etc/show-watcher.env
# Edit /etc/show-watcher.env with secrets and webhook URLs.
sudo cp deploy/show-watcher.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now show-watcher
```

The service file should run `WorkingDirectory=/opt/movie-tracker` and `ExecStart=/opt/movie-tracker/.venv/bin/python /opt/movie-tracker/web.py`. Persist `/opt/movie-tracker/data` across restarts and upgrades. Put the dashboard behind HTTPS or a VPN; set `COOKIE_SECURE=1` when HTTPS is enabled.

### Docker

```sh
docker build -t show-watcher .
docker run -d --restart unless-stopped -p 8080:8080 \
  --env-file .env -v "$PWD/data:/app/data" show-watcher
```

The Docker image uses Python 3.11 and does not copy `.env`. The LMDB directory must be mounted as persistent storage.

## Provider implementation

Provider-specific request parsing remains in `app.py`. Each adapter returns normalized `Listing` values, allowing the shared state comparison and Discord notification logic to stay provider-independent.
