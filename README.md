# ServerMonitor

A small, self-hosted, **read-only** host dashboard and symptom pager. Run one
command and get a single self-contained HTML page showing every component on a
Linux box — containers, systemd units, crons, nginx vhosts, ports, certs, DNS,
PM2, heartbeats — each coloured by its true state. A second command pages you
(Telegram, optional) only when something the owner would actually feel is
broken.

It runs on any Linux host with `python3` (stdlib only). Missing tools
(`docker`, `systemctl`, `nginx`, `certbot`, `pm2`, `apt`) are simply skipped —
nothing crashes.

> **Read-only, by construction.** These tools never start, stop, restart, delete
> or modify anything. They run read commands (`docker ps`, `systemctl show`,
> `crontab -l`, file reads) and write HTML/JSON output files. Every cleanup
> stays a human decision.

## Features

- **Inventory** (`inventory.py`) — one page, every component, three states:
  - **live** (green) — running / enabled / serving
  - **dormant** (grey) — present, off on purpose
  - **broken** (red) — actually wrong (failed unit, dead container, missing cron
    target, expiring cert)
  - plus an **abandoned** badge for leftovers worth reviewing
- **Monitoring** (`monitor.py`) — a table per monitor/job: what it watches,
  schedule, last run, last alert, live-or-broken. The monitor catalog is data
  (`config["monitors"]`), not code.
- **Needs attention** (`attention.py`) — every red item from both pages in one
  place, with red-count badges in the top nav.
- **Pager** (`alerts.py`) — pages only on symptoms, with debounce, cooldown and
  flapping protection. Telegram delivery is optional and off by default.
- **Heartbeats** (`hb.sh` + `heartbeats.json`) — dead-man's-switch: a job calls
  `hb.sh <name>` on success and the dashboard flags any heartbeat that goes
  stale.
- **Config-driven** — host name, thresholds, front-door domains, monitors and
  heartbeats all come from `config.json`. No host-specific values are baked in.
- **Zero dependencies** — Python stdlib only. No daemon. No database.

## Quickstart

```bash
git clone <your-fork> ServerMonitor && cd ServerMonitor
cp config.example.json config.json     # edit host_name, thresholds, monitors
python3 inventory.py                   # writes index.html
python3 monitor.py                     # writes monitor/index.html
python3 attention.py                   # writes attention/index.html + red-counts.json

# serve all three pages with the same paths the top nav expects:
python3 -m http.server 8080            # then open http://localhost:8080/
```

Preview a single page without serving:

```bash
python3 inventory.py --out /tmp/sm.html && xdg-open /tmp/sm.html
```

## Screenshot

```
┌────────────────────────────────────────────────────────────────────────────┐
│  Server inventory │   Monitoring   │   Needs attention (3)                  │
├───────────────┬────────────────────────────────────────────────────────────┤
│ On this page  │  Every component on this box                               │
│ All components│  generated 2026-01-01 00:00 UTC · myhost                   │
│ Heartbeats  2 │  ┌────────┬─────────┬────────┬───────┬───────────┐          │
│ Containers 12 │  │ Live 8 │ Dormant │ Broken │ Info  │ Abandoned │          │
│ Services   31 │  │   ●    │   2     │   3    │  3    │    4      │          │
│ Crons       7 │  └────────┴─────────┴────────┴───────┴───────────┘          │
│ Vhosts      5 │  ● nginx        file=enabled · active=active               │
│ Certs       2 │  ● web-1        Up 3 days · web:latest · 127.0.0.1:8080->80│
│ System      4 │  ● worker-1     Exited (1) · orphan container   [abandoned]│
└───────────────┴────────────────────────────────────────────────────────────┘
```

## The files

| File | Role |
|---|---|
| `inventory.py` | host dashboard generator → self-contained `index.html` |
| `monitor.py` | monitor/job table → `monitor/index.html` |
| `attention.py` | every red item in one page → `attention/index.html` |
| `alerts.py` | the symptom pager (+ `--dry`, `--digest`) |
| `redcounts.py` | red-item counts shared by the badges and attention page |
| `shell.py` | shared CSS tokens + top nav for all pages |
| `config.py` | the config loader (defaults ← `config.json`) |
| `config.example.json` | documented configuration template |
| `hb.sh` | heartbeat recorder (dead-man's switch) |
| `heartbeats.json` | example heartbeat registry |
| `make-sample.py` | generate a safe synthetic `sample-data.json` |
| `fonts.css` | embedded webfonts (no external requests) |

## Documentation

- **[ONBOARDING.md](ONBOARDING.md)** — install, configure, first run, serve it,
  wire up cron and Telegram.
- **[HOWTO.md](HOWTO.md)** — add a monitor, add a front-door domain, tune
  thresholds, run `alerts.py --dry`, customise the shell tokens.
- **[SAMPLE-DATA.md](SAMPLE-DATA.md)** — the dashboard data contract.

## License

MIT — see [LICENSE](LICENSE).
