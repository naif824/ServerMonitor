#!/usr/bin/env python3
"""Red-item counts and rows for the ServerMonitor pages.

Single source of truth for "what is red", shared by red-counts.json (the top-bar
badges) and attention.py (the Needs attention page). Presentation only: it reads
the generators' outputs and never changes any schema.

Sources (each optional; a missing source contributes nothing):
  index.json                 inventory.py --json
  monitor/index.json         monitor.py --json

Red rules:
  inventory  items with status == "broken"
  monitor    rows with status == "broken"
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.abspath(__file__))


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _embedded(path, varname):
    """Last-resort parse of `const NAME={...};` from a generated HTML page."""
    try:
        txt = open(path).read()
        m = re.search(r"const " + varname + r"=(\{.*?\});", txt, re.S)
        if m:
            return json.loads(m.group(1))
    except Exception:
        pass
    return None


def load_inventory(root=ROOT):
    d = _read_json(os.path.join(root, "index.json"))
    if d is None:
        d = _embedded(os.path.join(root, "index.html"), "INV")
    return d


def load_monitor(root=ROOT):
    for p in ("monitor/index.json", "monitor/monitor.json", "monitor-data.json"):
        d = _read_json(os.path.join(root, p))
        if d is not None and d.get("groups"):
            return d
    d = _embedded(os.path.join(root, "monitor", "index.html"), "DATA")
    return d if (d and d.get("groups")) else None


def collect(root=ROOT):
    """Return (counts, sources) where sources holds the extracted red rows."""
    inv = load_inventory(root)
    mon = load_monitor(root)

    inv_rows = []
    if inv:
        for c in inv.get("categories", []):
            for it in c.get("items", []):
                if it.get("status") != "broken":
                    continue
                name = re.sub(r"\s+\[.+\]$", "", it.get("name") or "")
                detail = " \u00b7 ".join(
                    [p for p in re.split(r"\s*\u00b7\s*", str(it.get("detail") or "")) if p][:3])
                inv_rows.append({"name": name, "where": c.get("label", ""),
                                 "detail": detail, "why": it.get("reason") or "broken"})

    mon_rows = []
    if mon:
        for g in mon.get("groups", []):
            for r in g.get("rows", []):
                if r.get("status") == "broken":
                    mon_rows.append({"name": r.get("name", ""), "where": g.get("label", ""),
                                     "detail": r.get("watches", ""),
                                     "why": "last run " + str(r.get("last") or "\u2014")})

    counts = {"inv": len(inv_rows), "mon": len(mon_rows)}
    counts["all"] = counts["inv"] + counts["mon"]
    sources = {"invRows": inv_rows, "monRows": mon_rows,
               "loaded": {"inv": inv is not None, "mon": mon is not None}}
    return counts, sources


def compute(root=ROOT):
    return collect(root)[0]


def write(root=ROOT):
    counts = compute(root)
    out = os.path.join(root, "red-counts.json")
    with open(out, "w") as f:
        json.dump(counts, f)
        f.write("\n")
    return counts


if __name__ == "__main__":
    c = write(ROOT)
    print("red-counts.json %s" % c)
