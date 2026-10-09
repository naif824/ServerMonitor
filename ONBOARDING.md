# Onboarding

Get ServerMonitor running on a fresh Linux host in a few minutes.

## Prerequisites

- **Linux** (any modern distro) with **`python3` 3.8+**. That is the only hard
  requirement — everything else is optional.
- Optionally installed tools it can use if present; each is skipped when absent,
  so there is no need to install any of them to get a working dashboard:
  - `docker` — container / image / volume / network inventory
  - `systemctl` — services, timers, sockets
  - `nginx` — vhost discovery (enabled vs `sites-available`)
  - `certbot` — certificate expiry (read via `sudo -n certbot certificates`)
  - `pm2` — process list
  - `named-checkzone` (BIND) — DNS zone validity
  - `crontab`, `/etc/cron.d`, `/etc/crontab` — cron inventory
- Optional: `curl` (the pager probes front-door domains over HTTPS).
- Optional: a Telegram bot (see below) if you want the pager to notify you.

Some checks use `sudo -n` (non-interactive) for read-only probes (`ss`, `certbot
certificates`, `stat`). Without passwordless sudo those probes simply return
nothing and the page is generated from what is readable.

## Install

```bash
git clone <your-fork> ServerMonitor
cd ServerMonitor
```

There is nothing to build and nothing to install — it is plain Python.

## Configure `config.json`

```bash
cp config.example.json config.json
```

Open `config.json` and set at least:

- **`host_name`** — what the pages call this box (e.g. `"web-01"`).
- **`thresholds`** — when the pager should fire:
  `disk_pct` (default 90), `mem_pct` (90), `load_per_core` (2.0),
  `swap_gb` (32), `cert_days` (7).
- **`front_doors`** — the public domains the pager probes. Leave it `[]` to
  discover them from enabled nginx vhosts automatically.
- **`monitors`** — your monitor/job catalog (see [HOWTO.md](HOWTO.md)).
- **`heartbeats`** — the dead-man's-switch registry (see below).

`config.json` is git-ignored, so your host details never get committed.

## First run

```bash
python3 inventory.py        # → index.html
python3 monitor.py          # → monitor/index.html
python3 attention.py        # → attention/index.html + red-counts.json
```

Each command prints a one-line summary. Open `index.html` in a browser to check
it looks right.

## Serving it

The three pages are plain files with hardcoded nav paths (`/`, `/monitor/`,
`/attention/`) plus `/red-counts.json`, so the simplest server is Python's
built-in one, run from the project directory:

```bash
python3 -m http.server 8080
# http://localhost:8080/
```

To keep it up, run it as a service (systemd or pm2) or point nginx at the
directory. A minimal nginx vhost:

```nginx
server {
    listen 80;
    server_name status.example.com;
    root /opt/ServerMonitor;
    index index.html;

    location / { try_files $uri $uri/ =404; }
    location = /red-counts.json { add_header Cache-Control "no-store"; }
}
```

If you do not need the nav to interlink, you can also just open the generated
`index.html` straight from disk — the pages still render; only the cross-page
links and the red-count badge fetch need a server.

## Regenerating on a schedule

Add a cron entry to keep the pages current (adjust the paths):

```cron
*/10 * * * *  cd /opt/ServerMonitor && /usr/bin/python3 inventory.py >/dev/null 2>&1 && /usr/bin/python3 monitor.py >/dev/null 2>&1 && /usr/bin/python3 attention.py >/dev/null 2>&1
*/10 * * * *  cd /opt/ServerMonitor && /usr/bin/python3 alerts.py >/dev/null 2>&1
0 6 * * 0     cd /opt/ServerMonitor && /usr/bin/python3 alerts.py --digest >/dev/null 2>&1
```

The generator must run before `alerts.py` — the pager reuses the inventory's
`index.json` for cert data and its self-check looks at how recently the page was
generated.

## Wiring the pager to Telegram

Delivery is **off by default**. To enable it:

1. Talk to [@BotFather](https://t.me/BotFather) on Telegram, create a bot, and
   copy its **bot token**.
2. Send your new bot a message, then find your **chat id** (e.g. message
   [@userinfobot](https://t.me/userinfobot), or read
   `https://api.telegram.org/bot<TOKEN>/getUpdates`).
3. Put both in `config.json`:

   ```json
   "telegram": { "bot_token": "123456:ABC-your-token", "chat_id": "123456789" }
   ```

4. Optionally set `status_url` to the dashboard URL, so paged messages link back
   to it.
5. Test without sending anything:

   ```bash
   python3 alerts.py --dry        # prints what it would send
   ```

6. Once you are happy, let cron run `alerts.py` every couple of minutes (see
   above). The weekly `--digest` is the all-clear whose **absence** is itself the
   alarm for the notification channel.

## Heartbeats (dead-man's switch)

Any cron job can prove it ran by calling the recorder as its last step:

```bash
/path/to/myjob.sh && /home/you/ServerMonitor/hb.sh nightly-backup
```

`hb.sh` writes a timestamp to `state/hb/<name>`. Declare the name and how stale
it may get in `config.json` (`heartbeats`), or in `heartbeats.json`:

```json
"nightly-backup": { "max_age_min": 1800, "desc": "encrypted nightly backup (01:00)" }
```

If the heartbeat goes older than `max_age_min`, it turns red on the dashboard and
the pager can notify you — before you actually need the backup.

## Where state lives

Everything generated or recorded lives under `state/` (or wherever
`base_dir` points), and `config.json` is git-ignored. Nothing is written outside
the project directory.
