#!/usr/bin/env python3
"""config.py — the single configuration loader for ServerMonitor.

Every generator imports this module instead of hardcoding host-specific values.
It reads ``config.json`` from this package directory and falls back to
``config.example.json`` when ``config.json`` is absent, then merges whatever it
finds over the built-in defaults below. An optional ``--config PATH`` argument,
or ``SERVERMONITOR_CONFIG`` in the environment, overrides the search.

There is nothing secret in the defaults: a fresh clone runs read-only with no
``config.json`` at all.

Usage:
    import config
    config.HOST, config.BASE, config.STATE, config.threshold("disk_pct", 90)
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# Built-in defaults. Every one of these can be overridden in config.json.
DEFAULTS = {
    "host_name": "myhost",
    "base_dir": ".",
    "status_url": "",
    "telegram": {"bot_token": "", "chat_id": ""},
    "thresholds": {
        "disk_pct": 90,
        "mem_pct": 90,
        "load_per_core": 2.0,
        "swap_gb": 32,
        "cert_days": 7,
    },
    "front_doors": [],
    "monitors": [],
    "heartbeats": {},
}


def _deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _default_path():
    env = os.environ.get("SERVERMONITOR_CONFIG")
    if env:
        return env
    for name in ("config.json", "config.example.json"):
        p = os.path.join(HERE, name)
        if os.path.isfile(p):
            return p
    return None


def load(path=None):
    """Return the resolved config dict (defaults <- config.json <- example)."""
    cfg = json.loads(json.dumps(DEFAULTS))
    p = path or _default_path()
    if p and os.path.isfile(p):
        try:
            with open(p) as f:
                cfg = _deep_merge(cfg, json.load(f))
        except Exception as e:                       # never crash a read-only tool
            print("config: could not read %s: %s" % (p, e))
    return cfg


CFG = load()

# ---- resolved, ready-to-use values -----------------------------------------
HOST = str(CFG.get("host_name") or "myhost")
HOST_BADGE = HOST[:3].lower()
STATUS_URL = CFG.get("status_url") or ""
BASE = os.path.abspath(os.path.join(HERE, CFG.get("base_dir") or "."))
STATE = os.path.join(BASE, "state")


def threshold(name, default):
    v = (CFG.get("thresholds") or {}).get(name)
    return default if v is None else v


def telegram():
    """(bot_token, chat_id) — empty strings when not configured."""
    t = CFG.get("telegram") or {}
    return t.get("bot_token") or "", t.get("chat_id") or ""


def front_doors():
    """Configured front-door domains (empty list -> discover from nginx)."""
    return [d for d in (CFG.get("front_doors") or []) if d]


def monitors():
    """Configured monitor/job entries (see config.example.json)."""
    return list(CFG.get("monitors") or [])


def heartbeats():
    """Heartbeat registry: heartbeats.json merged with config['heartbeats']."""
    reg = {}
    p = os.path.join(HERE, "heartbeats.json")
    try:
        with open(p) as f:
            reg = json.load(f)
    except Exception:
        reg = {}
    reg.update(CFG.get("heartbeats") or {})
    return reg
