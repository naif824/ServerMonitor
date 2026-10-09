# Dashboard data contract

`sample-data.json` is the exact JSON the dashboard embeds and renders
client-side. The copy in this repo is **synthetic demo data** — no real host is
described. Regenerate it any time with `python3 make-sample.py`; snapshot your
own machine with `python3 inventory.py --json && python3 make-sample.py --live`
(and do not commit that output).

## Shape

```jsonc
{
  "host": "example-host",
  "generated_at": "2026-01-01 00:00:00 UTC",
  "counts": { "live": 8, "dormant": 2, "broken": 3, "info": 3 },
  "abandoned": 4,
  "categories": [
    { "key": "containers", "label": "Containers",
      "items": [ { "cat":"containers","name":"web-1","status":"live",
                   "detail":"Up 3 days · web:latest · app_default · 127.0.0.1:8080->80",
                   "abandoned": false, "reason": "", "meta": { "scope": "local" } } ] }
  ]
}
```

## Fields

- **status** — one of four:
  - `live` (green) — up / enabled / serving now
  - `dormant` (grey) — off on purpose (disabled unit, retired route)
  - `broken` (red) — actually wrong (failed unit, dead container, missing cron target, expiring cert)
  - `info` (blue) — neutral (default networks, system uptime, undocumented-but-known)
- **abandoned** (bool) — leftover worth reviewing → amber badge; `reason` says why.
- **detail** — the human line under the name (owner · folder · domain, etc.).
- **meta.scope** — `local` (yours) or `vendor` (OS plumbing); the "Mine only" toggle filters on this.
- **cat** — category key; **key/label** on the category object.

## UI that consumes this

- **Header**: `host`, `generated_at`, and the four `counts` + `abandoned` as chips.
- **Two filter rows**: a **component** row (one button per `categories[].key`, live counts) above a **status** row (All/Live/Dormant/Broken/Abandoned) + **Mine only** + a name **search**.
- Rows render: status dot · name (+ abandoned badge + reason) · detail.
- Empty-state per category: "none found on this box" vs "all filtered out…".
