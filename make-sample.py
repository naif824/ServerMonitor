#!/usr/bin/env python3
"""Generate a SAFE sample-data.json for the dashboard.

By default this writes a small SYNTHETIC inventory (fake hosts, containers,
services) using the exact schema the dashboard consumes — safe to commit and
share. Pass --live to snapshot a real ``index.json`` instead (do NOT commit
that output; it reflects the machine it ran on).

Regenerate the synthetic sample:  python3 make-sample.py
Snapshot your own live box:      python3 inventory.py --json && python3 make-sample.py --live
"""
import argparse
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "index.json")
OUT = os.path.join(HERE, "sample-data.json")

_NOTE = ("Synthetic demo data — NOT a real host. Same schema the dashboard "
         "consumes. Regenerate with make-sample.py (or --live for a real "
         "snapshot you should not commit).")


def _it(cat, name, status, detail, abandoned=False, reason="", **meta):
    meta.setdefault("scope", "local")
    return {"cat": cat, "name": name, "status": status, "detail": detail,
            "abandoned": bool(abandoned), "reason": reason, "meta": meta}


def synthetic():
    cats = [
        ("heartbeats", "Heartbeats", [
            _it("heartbeats", "nightly-backup", "live", "ok · 42m ago · encrypted nightly backup"),
            _it("heartbeats", "weekly-verify", "info", "awaiting first run · weekly restore verification"),
        ]),
        ("containers", "Containers", [
            _it("containers", "web-1", "live", "Up 3 days · web:latest · app_default · 127.0.0.1:8080->80"),
            _it("containers", "worker-1", "broken", "Exited (1) 20 minutes ago · worker:latest", True,
                "orphan container (not in any compose file)", image="worker:latest", state="exited"),
        ]),
        ("services", "Systemd services", [
            _it("services", "nginx", "live", "file=enabled · active=active · vendor"),
            _it("services", "myapp", "broken", "file=enabled · active=failed · local",
                False, "enabled but not running", file_state="enabled", active="failed",
                local=True, scope="local"),
        ]),
        ("crons", "Cron jobs", [
            _it("crons", "backup.sh  [user crontab]", "info", "0 1 * * * · user crontab",
                source="user crontab", schedule="0 1 * * *"),
            _it("crons", "sync.sh  [user crontab]", "broken", "*/5 * * * * · user crontab", True,
                "target missing: /opt/app/sync.sh", source="user crontab", schedule="*/5 * * * *"),
        ]),
        ("nginx", "Nginx vhosts", [
            _it("nginx", "example.com", "live", "enabled and served"),
            _it("nginx", "old.example.com", "dormant", "available but NOT enabled (orphaned)", True,
                "no symlink in sites-enabled"),
        ]),
        ("certs", "Certificates", [
            _it("certs", "example.com", "live", "expires in 71d · example.com www.example.com",
                days=71, referenced=True),
            _it("certs", "legacy", "dormant", "expires in 30d · legacy.example.com", True,
                "not referenced by any enabled vhost", days=30, referenced=False),
        ]),
        ("system", "System health", [
            _it("system", "disk /", "live", "38% used of 80GB"),
            _it("system", "memory", "live", "41% used of 16GB"),
            _it("system", "load", "live", "0.42 on 4 cores"),
            _it("system", "uptime", "info", "12d"),
        ]),
    ]
    counts = {"live": 0, "dormant": 0, "broken": 0, "info": 0}
    abandoned = 0
    for _k, _l, items in cats:
        for it in items:
            counts[it["status"]] = counts.get(it["status"], 0) + 1
            if it["abandoned"]:
                abandoned += 1
    return {
        "host": "example-host",
        "generated_at": "2026-01-01 00:00:00 UTC",
        "counts": counts,
        "abandoned": abandoned,
        "categories": [{"key": k, "label": l, "items": items} for k, l, items in cats],
        "_note": _NOTE,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="snapshot a real index.json instead of synthetic data")
    args = ap.parse_args()
    if args.live:
        inv = json.load(open(SRC))
        inv["_note"] = ("Real snapshot of this host's live inventory. Do NOT "
                        "commit this file. Regenerate with make-sample.py --live.")
    else:
        inv = synthetic()
    json.dump(inv, open(OUT, "w"), indent=1, ensure_ascii=False)
    print(f"wrote {OUT} — {len(inv['categories'])} categories (synthetic={not args.live})")


if __name__ == "__main__":
    main()
