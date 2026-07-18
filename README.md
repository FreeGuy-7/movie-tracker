# Show Watcher

A lightweight, dependency-free Python web app for watching movie listings. It currently supports District and runs its own continuous scheduler; BookMyShow/PVR adapters can be added later.

## Web dashboard

Run `python3 web.py`, then open `http://localhost:8080`. Add a District movie URL, target date, city location, and a check frequency (minimum five minutes). The server checks each trigger continuously, persists it in `data/triggers.json`, and records listing state in `data/state.json`.

Set `DISCORD_WEBHOOK_URL` before starting the server to enable notifications. Every successful trigger run sends a report grouped by format (such as IMAX or 4DX) and then cinema. The first successful check establishes a baseline; later newly added showtimes generate a separate tagged alert. The default tag is `@here`; set `DISCORD_MENTION` to `<@your-user-id>` or a role mention to target a specific recipient. The service also sends a running-status heartbeat every 60 minutes by default. For testing, set `HEARTBEAT_MINUTES=1`. Set `APP_PASSWORD` before exposing the dashboard publicly; it enables browser Basic Authentication with username `watcher`.

Do not paste browser cookies, guest tokens, or request IDs into the app. District's anonymous token is generated for each request.

## Deployment

**Recommended: Oracle Cloud Always Free VM.** It supports an always-on process and persistent local files, which this scheduler needs. Oracle currently offers Always Free AMD micro VMs and Ampere A1 capacity (up to 2 OCPUs and 12 GB total), though free-shape capacity can be unavailable in some regions. [Oracle Free Tier](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier.htm) and [Always Free compute limits](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm).

1. Create an Ubuntu Always Free VM and open its web port only through a reverse proxy or VPN; do not expose an unprotected dashboard.
2. Copy this project to the VM and set `APP_PASSWORD` and `DISCORD_WEBHOOK_URL` in its environment.
3. Copy `deploy/show-watcher.service` to `/etc/systemd/system/`, create `/etc/show-watcher.env`, then run `sudo systemctl enable --now show-watcher`.
4. Keep the `data/` directory on the VM's block volume; it holds triggers and notification state.

Example `/etc/show-watcher.env`:

```sh
APP_PASSWORD=choose-a-strong-password
DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/...
PORT=8080
HEARTBEAT_MINUTES=60
DISCORD_MENTION=@here
```

For the simplest managed demo, Render's free web services spin down after 15 minutes of inactivity, so they are not suitable for this continuous scheduler. Its always-on services are paid. [Render pricing overview](https://render.com/articles/how-much-does-cloud-application-hosting-cost-for-small-businesses). A Raspberry Pi or NAS already running at home is the practical zero-extra-cost alternative.

### Docker

Build with `docker build -t show-watcher .`. Run with persistent local storage:

```sh
docker run -d --restart unless-stopped -p 8080:8080 \
  -e APP_PASSWORD='choose-a-strong-password' \
  -e DISCORD_WEBHOOK_URL='https://discord.com/api/webhooks/...' \
  -v "$PWD/data:/app/data" show-watcher
```

## Configuration

`config.json` remains supported as a one-time bootstrap: its watches are loaded into the dashboard when `data/triggers.json` does not yet exist. After that, manage triggers from the UI.

## Extending providers

Provider-specific fetching is isolated in `fetch_listing()`. Add provider adapters that return normalized `Listing` values, then reuse the existing state comparison and Discord notification flow.
