# HOWTO

Common tasks. All commands are run from the project directory.

## Add a monitor

Monitors are **data**, not code. Add an entry to `config.json` → `monitors` and
it appears on the Monitoring page the next time `monitor.py` runs. Nothing needs
editing in the Python.

```json
{
  "name": "nightly-backup.sh",
  "group": "Backups",
  "what": "encrypted nightly filesystem backup",
  "schedule": "daily 01:00 UTC",
  "script": "/opt/backup/nightly-backup.sh",
  "log": "/var/log/nightly-backup.log",
  "heartbeat": "nightly-backup",
  "every_min": 1560
}
```

| Field | Required | Meaning |
|---|---|---|
| `name` | yes | Row label. |
| `group` | no | Section on the page (default `Monitors`). Groups appear in the order first seen in the config. |
| `what` | no | What it watches (the "watches" column). |
| `schedule` | no | Human schedule text. If omitted, `monitor.py` tries to find it in the crontab entry that runs `script`. |
| `script` | no | Path to the script — shown under the name (`~` abbreviated). Also used for schedule lookup. |
| `log` | no | Log file to read for "last run" and alert-looking lines. |
| `heartbeat` | no | A heartbeat name (see below). **Preferred** source of liveness. |
| `every_min` | no | Expected cadence in minutes. When `log` is older than this, the row goes **broken**. |

Liveness is decided in this order: `heartbeat` (if set) → `log` freshness against
`every_min` → `info` (passive). To turn a row red when a job stops writing its
log, set `every_min`.

## Add a front-door domain

The pager probes the domains in `config.json` → `front_doors`:

```json
"front_doors": ["example.com", "www.example.com", "api.example.com"]
```

Each is fetched over HTTPS; **no response or a 5xx** pages you ("front door
example.com not serving"). A 2xx, 3xx or 4xx counts as serving.

Leave `front_doors` as `[]` to auto-discover domains from enabled nginx vhosts
(`server_name` directives in `/etc/nginx/sites-enabled`).

## Tune thresholds

`config.json` → `thresholds`:

```json
"thresholds": {
  "disk_pct": 90,
  "mem_pct": 90,
  "load_per_core": 2.0,
  "swap_gb": 32,
  "cert_days": 7
}
```

- `disk_pct` / `mem_pct` — page when the root filesystem / RAM is at or above this percent.
- `load_per_core` — page when 5-minute load per CPU core is at or above this.
- `swap_gb` — page when used swap is at or above this many GB (memory pressure).
- `cert_days` — page when a live, referenced certificate expires within this many days.

The dashboard and the pager read the **same** values, so they never disagree.
After changing them, re-run `inventory.py`/`monitor.py` to refresh the pages.

## Run the pager in dry mode

`--dry` prints exactly what would be sent and **never sends and never records
state**, so it is safe to run anywhere:

```bash
python3 alerts.py --dry           # evaluate + show any pages/recoveries
python3 alerts.py --dry --digest  # show the weekly all-clear text
```

Interpret the output:

- `quiet (N symptoms, none newly pageable)` — nothing to do.
- `--- SIGNAL (dry) pager/info ---` — the batched human message.
- `--- SIGNAL (dry) pager/action ---` — one line per paged symptom.

Anti-noise: a symptom must persist for **≥5 minutes** before paging (debounce),
then goes quiet for **~1 hour** (cooldown). A component that breaks/recoveries
≥3 times in an hour is reported once as "flapping". Intentional stops can be
listed in `suppress.json` (fnmatch patterns) so they never page.

## Customise the shell tokens

All three pages share one design-token block in `shell.py` (`TOKENS`) plus the
top-nav definition (`TABS`, `topbar`). To re-skin:

1. Edit the CSS custom properties in `shell.py` → `TOKENS`. The light theme is
   the `:root{…}` block; the dark theme is `:root[data-theme="dark"]{…}`.
2. Re-run the generators; the new values are inlined into every page.

The most useful tokens:

| Token | Use |
|---|---|
| `--bg`, `--surface`, `--surface2` | page / card / sidebar backgrounds |
| `--text`, `--text2`, `--muted`, `--faint` | text ramp |
| `--live`, `--dormant`, `--broken`, `--info` | status dot colours |
| `--*-t` (e.g. `--broken-t`) | the matching tinted background |
| `--amber`, `--amber-t` | the "abandoned" badge |
| `--action`, `--on-action` | active/selected button colours |

The dark theme is chosen automatically from the viewer's local time-of-day, so
keep both blocks in sync when you change colours.

## Reset generated state

Generated HTML/JSON and recorded state live under `state/` and the page output
files. They are all safe to delete; the next run recreates them:

```bash
rm -rf state/ red-counts.json monitor/index.html monitor/index.json index.html index.json attention/index.html
```
