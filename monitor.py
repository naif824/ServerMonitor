#!/usr/bin/env python3
"""ServerMonitor monitor pane — one read-only view of the monitoring layer on a box.

Answers, for every monitor/job:  name | what it watches | schedule | last run
| last alert | live-or-broken.  It does NOT re-implement discovery: it imports
inventory.py and reuses its cron / systemd / heartbeat / system readers, and
reuses the same fonts + CSS so the page matches the rest of the dashboard.

The monitor list itself is DATA, not code: it comes from ``config["monitors"]``
(see config.example.json). No host-specific paths are baked in.

Read-only. It never starts, stops, restarts or deletes anything.

Usage:
  python3 monitor.py                      # writes monitor/index.html next to this file
  python3 monitor.py --out /tmp/x.html --json
"""
import argparse, html, json, os, re, sys, time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config
import inventory as inv                       # reuse discovery + styling (never rewrite)
import shell

OUT_DIR    = os.path.join(HERE, "monitor")
STATE      = config.STATE
ALERTS_ST  = os.path.join(STATE, "alerts.json")      # the pager's per-item state
INCIDENTS  = os.path.join(STATE, "incidents")        # symptom files the pager drops
HB_DIR     = os.path.join(STATE, "hb")               # heartbeat stamps
HEARTBEATS = os.path.join(HERE, "heartbeats.json")
INDEX      = os.path.join(config.BASE, "index.json")


# ---------------------------------------------------------------- small helpers
def now():
    return time.time()


def mtime(p):
    try:
        return os.path.getmtime(p)
    except Exception:
        return None


def read_json(p, default):
    try:
        return json.load(open(p))
    except Exception:
        return default


def rel(epoch):
    if not epoch:
        return "—"
    s = int(max(0, now() - epoch))
    if s < 60:
        return f"{s}s ago"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def esc(s):
    return html.escape(str(s if s is not None else ""))


# ---------------------------------------------------------------- cron schedule lookup
def _cron_entries():
    try:
        return list(inv._cron_entries())          # (source, schedule, command, line_no)
    except Exception:
        return []


_CRONS = None


def schedule_for(path):
    """The cron schedule whose command runs this exact script, or None."""
    global _CRONS
    if _CRONS is None:
        _CRONS = _cron_entries()
    for _src, sched, cmd, _ln in _CRONS:
        if path and path in cmd:
            return sched.strip()
    return None


def schedule_like(path, needle):
    """Schedule of the cron whose command contains `needle` (for compound lines)."""
    global _CRONS
    if _CRONS is None:
        _CRONS = _cron_entries()
    for _src, sched, cmd, _ln in _CRONS:
        if needle in cmd:
            return sched.strip()
    return None


# ---------------------------------------------------------------- heartbeats
_HB = None


def hb_registry():
    global _HB
    if _HB is None:
        _HB = config.heartbeats()
    return _HB


def hb_state(name):
    """(last_epoch, max_age_min, age_min) — all None if it never ran."""
    p = os.path.join(HB_DIR, name)
    if not os.path.exists(p):
        return None, None, None
    try:
        last = float(open(p).read().strip())
    except Exception:
        last = mtime(p)
    maxmin = hb_registry().get(name, {}).get("max_age_min", 120)
    return last, maxmin, (now() - last) / 60.0


# ---------------------------------------------------------------- alerts
def _alert_map():
    """key -> newest alert epoch, from the pager state + queued/consumed incidents."""
    m = {}
    st = read_json(ALERTS_ST, {})
    for k, rec in (st.items() if isinstance(st, dict) else []):
        lp = rec.get("last_page") or 0
        if lp:
            m[k] = max(m.get(k, 0), lp)
    for sub in ("", "done"):
        d = os.path.join(INCIDENTS, sub) if sub else INCIDENTS
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if not f.endswith(".json"):
                continue
            rec = read_json(os.path.join(d, f), {})
            k = rec.get("key")
            if k:
                m[k] = max(m.get(k, 0), rec.get("ts") or 0)
    return m


_AMAP = None


def pager_alert(keys):
    global _AMAP
    if _AMAP is None:
        _AMAP = _alert_map()
    best = 0
    for k, ts in _AMAP.items():
        for key in keys:
            if key.endswith("*"):
                if k.startswith(key[:-1]):
                    best = max(best, ts)
            elif k == key:
                best = max(best, ts)
    return best or None


_ALERT_RE = re.compile(r"(🔴|🚨|⚠|✗|\bALERT\b|\bFAIL(?:ED)?\b|\bDOWN\b|\bCRITICAL\b|\bTraceback\b|\bstale\b)")


def log_alert(path):
    """(epoch, snippet) for the newest alert-looking line in a log's tail, or (None, None)."""
    if not path or not os.path.isfile(path):
        return None, None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - 80000))
            tail = f.read().decode("utf-8", "ignore")
    except Exception:
        return None, None
    hit = None
    for ln in tail.splitlines():
        if _ALERT_RE.search(ln):
            hit = ln.strip()[:150]
    return (mtime(path) if hit else None), hit


def last_alert(keys=(), logpath=None):
    """Newest of: the pager's alert for `keys`, this monitor's own log alert."""
    pa = pager_alert(keys) if keys else None
    la, snippet = (None, None)
    if logpath:
        la, snippet = log_alert(logpath)
    best, src, txt = 0, None, None
    if pa:
        best, src, txt = pa, "pager", rel(pa) + " (pager)"
    if la and la > best:
        best, src, txt = la, "log", rel(la) + " (log)"
    if not best:
        return None, "—", None
    if src == "log" and snippet:
        txt += " · " + snippet
    return best, txt, src


# ---------------------------------------------------------------- row model
def make_row(group, name, watches, schedule, status, *, last_ts=None,
             last_txt=None, alert_ts=None, alert_txt=None, path=""):
    return {
        "group": group, "name": name, "watches": watches, "schedule": schedule or "—",
        "status": status, "path": path,
        "last_ts": last_ts or 0,
        "last": last_txt if last_txt is not None else rel(last_ts),
        "alert_ts": alert_ts or 0,
        "alert": alert_txt if alert_txt is not None else rel(alert_ts),
    }


def status_for_log(logpath, expect_s, heartbeat=None):
    """live/broken/info from a heartbeat (preferred) or a log's freshness."""
    if heartbeat:
        last, maxmin, age = hb_state(heartbeat)
    if heartbeat and last is None:
        return "info"
    if heartbeat:
        return "broken" if age > (maxmin or 120) else "live"
    lm = mtime(logpath) if logpath else None
    if lm is None:
        return "info"
    if expect_s and (now() - lm) > expect_s:
        return "broken"
    return "live"


def build_monitor_row(group, name, path, watches, *, schedule=None, heartbeat=None,
                      logpath=None, expect_s=None, alert_keys=(), custom_status=None,
                      last_ts=None, last_txt=None, note_schedule=None):
    sched = schedule or schedule_for(path) or "—"
    st = custom_status or status_for_log(logpath, expect_s, heartbeat)
    # last run
    if heartbeat:
        last, maxmin, age = hb_state(heartbeat)
        if last is None:
            last_txt = "awaiting first run"
        else:
            last_txt = rel(last) + f" · hb ≤{maxmin // 60}h" if maxmin and maxmin >= 60 else rel(last) + f" · hb ≤{maxmin}m"
        last_ts = last or 0
    elif logpath and mtime(logpath):
        last_ts = mtime(logpath)
        last_txt = rel(last_ts)
    # last alert
    ats, atxt, _src = last_alert(alert_keys, logpath)
    return make_row(group, name, watches, sched, st, last_ts=last_ts,
                    last_txt=last_txt, alert_ts=ats, alert_txt=atxt, path=path)


# ---------------------------------------------------------------- groups
def group_host():
    rows = []
    sched = "every pager cycle (2 min)"
    sysitems = {}
    try:
        for it in inv.discover_system():
            sysitems[it["name"]] = it
    except Exception:
        pass

    def alert_for(keys):
        ats, atxt, _ = last_alert(keys)
        return ats, atxt

    disk = sysitems.get("disk /")
    a_ts, a_txt = alert_for(("disk:/",))
    rows.append(make_row("host", "disk /", f"root filesystem fills up (pages at ≥{config.threshold('disk_pct', 90):g}%)",
                         sched, disk["status"] if disk else "info",
                         last_txt=disk["detail"] if disk else "—",
                         alert_ts=a_ts, alert_txt=a_txt))
    mem = sysitems.get("memory")
    rows.append(make_row("host", "memory", f"RAM pressure (pages at ≥{config.threshold('mem_pct', 90):g}%)",
                         sched, mem["status"] if mem else "info",
                         last_txt=mem["detail"] if mem else "—"))
    load = sysitems.get("load")
    a_ts, a_txt = alert_for(("load:/",))
    rows.append(make_row("host", "load", f"host oversubscribed (pages at ≥{config.threshold('load_per_core', 2.0):g}/core)",
                         sched, load["status"] if load else "info",
                         last_txt=load["detail"] if load else "—",
                         alert_ts=a_ts, alert_txt=a_txt))
    # swap (same source + threshold the pager uses)
    swap_gb = config.threshold("swap_gb", 32)
    swap_detail, swap_status, swap_ts, swap_txt = "—", "live", None, "—"
    try:
        mi = {}
        for line in open("/proc/meminfo"):
            if ":" in line:
                k, v = line.split(":", 1)
                mi[k] = int(v.split()[0])
        used_g = (mi["SwapTotal"] - mi["SwapFree"]) / (1024 * 1024)
        swap_detail = f"{used_g:.0f}G used (pages at ≥{swap_gb:g}G)"
        swap_status = "broken" if used_g >= swap_gb else "live"
        swap_ts, swap_txt, _ = last_alert(("swap:/",))
    except Exception:
        pass
    rows.append(make_row("host", "swap", "swap pressure / memory thrash",
                         sched, swap_status, last_txt=swap_detail,
                         alert_ts=swap_ts, alert_txt=swap_txt))
    # uptime (compute directly — /proc/uptime is seconds since boot)
    up_txt = "—"
    try:
        secs = float(open("/proc/uptime").read().split()[0])
        up_txt = inv.age_from_seconds(secs) + " up"
    except Exception:
        pass
    rows.append(make_row("host", "uptime", "how long since last boot",
                         sched, "info", last_txt=up_txt))
    # failed systemd services
    failed = []
    try:
        failed = [i["name"] for i in inv.discover_units("services", ".service")
                  if i["status"] == "broken"]
    except Exception:
        pass
    a_ts, a_txt = alert_for(("service:*",))
    rows.append(make_row("host", "systemd failed services",
                         "enabled units in failed state",
                         sched, "broken" if failed else "live",
                         last_txt=(f"{len(failed)} failed: " + ", ".join(failed[:3])) if failed else "none failed",
                         alert_ts=a_ts, alert_txt=a_txt))
    return rows


def group_pager_agent():
    rows = []
    # inventory.py — the generator
    idx_age = (now() - mtime(INDEX)) if mtime(INDEX) else None
    inv_status = "broken" if (idx_age is None or idx_age > 300) else "live"
    inv_last = rel(mtime(INDEX)) if mtime(INDEX) else "never"
    ats, atxt, _ = last_alert(("monitor:stale",))
    rows.append(make_row("pager", "inventory.py", "every component on this box — the generator behind this dashboard",
                         schedule_for(os.path.join(HERE, "inventory.py")), inv_status,
                         last_ts=mtime(INDEX), last_txt=inv_last,
                         alert_ts=ats, alert_txt=atxt, path=os.path.join(HERE, "inventory.py")))
    # alerts.py — the pager
    a_age = (now() - mtime(ALERTS_ST)) if mtime(ALERTS_ST) else None
    a_status = "broken" if (a_age is None or a_age > 600) else "live"
    am = _alert_map()
    any_alert = max(am.values()) if am else None
    rows.append(make_row("pager", "alerts.py — the pager",
                         "front doors, containers, services, heartbeats, certs, disk, swap, load + self-check",
                         schedule_for(os.path.join(HERE, "alerts.py")) or "*/2",
                         a_status, last_ts=mtime(ALERTS_ST),
                         last_txt=rel(mtime(ALERTS_ST)) if mtime(ALERTS_ST) else "never",
                         alert_ts=any_alert,
                         alert_txt=(rel(any_alert) + " (newest paged)") if any_alert else "—",
                         path=os.path.join(HERE, "alerts.py")))
    # hb.sh — the recorder
    newest = 0
    try:
        for f in os.listdir(HB_DIR):
            newest = max(newest, mtime(os.path.join(HB_DIR, f)) or 0)
    except Exception:
        pass
    rows.append(make_row("pager", "hb.sh — heartbeat recorder",
                         "records job success into state/hb/ (the dead-man's switch the pager reads)",
                         "called by each job on success",
                         "live" if newest and (now() - newest) < 3600 else ("info" if not newest else "broken"),
                         last_ts=newest, last_txt=rel(newest) if newest else "never",
                         path=os.path.join(HERE, "hb.sh")))
    return rows


def group_heartbeats():
    rows = []
    for name, meta in hb_registry().items():
        last, maxmin, age = hb_state(name)
        watches = meta.get("desc", "")
        if maxmin:
            watches = (watches + f" · dead-man switch: stale after {maxmin}m").strip(" ·")
        if last is None:
            st, last_txt = "info", "awaiting first run"
        else:
            st = "broken" if age > (maxmin or 120) else "live"
            last_txt = rel(last)
        ats, atxt, _ = last_alert((f"hb:{name}",))
        rows.append(make_row("heartbeats", name, watches, "—", st, last_ts=last,
                             last_txt=last_txt, alert_ts=ats, alert_txt=atxt))
    return rows


def _configured_rows(entries):
    """Rows for one config["monitors"] group (see config.example.json)."""
    rows = []
    for m in entries:
        every = m.get("every_min")
        hb = m.get("heartbeat") or None
        rows.append(build_monitor_row(
            m.get("group") or "monitors",
            m.get("name") or "monitor",
            m.get("script") or "",
            m.get("what") or "",
            schedule=m.get("schedule"),
            heartbeat=hb,
            logpath=m.get("log") or None,
            expect_s=int(every * 60) if every else None,
            alert_keys=(f"hb:{hb}",) if hb else (),
        ))
    return rows


def _slug(s, i):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-") or ("grp%d" % i)


def configured_groups():
    """(key, label, fn) for every distinct group named in config["monitors"].

    The monitor catalog is data, not code: add an entry to config.json and it
    appears here. Group order follows first appearance in the config.
    """
    order, by = [], {}
    for m in config.monitors():
        g = (m.get("group") or "Monitors").strip() or "Monitors"
        if g not in by:
            by[g] = []
            order.append(g)
        by[g].append(m)
    return [(  _slug(g, i), g, (lambda items: (lambda: _configured_rows(items)))(by[g]))
            for i, g in enumerate(order)]


BUILTIN_GROUPS = [
    ("pager", "Pager & Agent", group_pager_agent),
    ("host", "Host", group_host),
    ("heartbeats", "Heartbeats", group_heartbeats),
]


def all_groups():
    return BUILTIN_GROUPS + configured_groups()


def collect():
    groups = []
    counts = {"live": 0, "broken": 0, "info": 0}
    for key, label, fn in all_groups():
        try:
            rows = fn()
        except Exception as e:
            rows = [make_row(key, "(collection error)", str(e), "—", "broken")]
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        groups.append({"key": key, "label": label, "rows": rows})
    return {
        "host": config.HOST,
        "generated_at": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "counts": counts,
        "groups": groups,
    }


# ---------------------------------------------------------------- render
STATUS_COL = {"live": "var(--live)", "broken": "var(--broken)", "info": "var(--info)"}
STATUS_RING = {"live": "var(--live-t)", "broken": "var(--broken-t)", "info": "var(--info-t)"}


def _row_html(r):
    col = STATUS_COL.get(r["status"], "var(--info)")
    ring = STATUS_RING.get(r["status"], "var(--info-t)")
    path = f'<div class="mono path">{esc(r["path"].replace(os.path.expanduser("~") + "/", "~/"))}</div>' if r["path"] else ""
    badge_bg = {"live": "var(--live-t)", "broken": "var(--broken-t)", "info": "var(--info-t)"}.get(r["status"], "var(--info-t)")
    badge_fg = {"live": "var(--live)", "broken": "var(--broken)", "info": "var(--info)"}.get(r["status"], "var(--info)")
    return (
        f'<div class="row hov" data-status="{r["status"]}" data-name="{esc(r["name"]).lower()} {esc(r["watches"]).lower()}">'
        f'<div class="cdot"><span style="background:{col};box-shadow:0 0 0 3px {ring}"></span></div>'
        f'<div class="cname"><div class="nm">{esc(r["name"])}</div>{path}</div>'
        f'<div class="cwatch">{esc(r["watches"])}</div>'
        f'<div class="csch mono">{esc(r["schedule"])}</div>'
        f'<div class="clast mono">{esc(r["last"])}</div>'
        f'<div class="calert mono">{esc(r["alert"])}</div>'
        f'<div class="cstat"><span class="badge" style="background:{badge_bg};color:{badge_fg}">{r["status"]}</span></div>'
        f'</div>'
    )


def _group_html(g):
    rows = "".join(_row_html(r) for r in g["rows"])
    br = sum(1 for r in g["rows"] if r["status"] == "broken")
    badge = f'<span class="gbad">{br}</span>' if br else ""
    return (
        f'<section class="grp" id="g-{g["key"]}" data-grp="{g["key"]}">'
        f'<div class="grphead"><div class="ghtitle"><h2>{esc(g["label"])}</h2>{badge}</div>'
        f'<span class="mono gcount" data-count="{g["key"]}">{len(g["rows"])}</span></div>'
        f'<div class="tbl">'
        f'<div class="thead mono"><div></div><div>Monitor</div><div>What it watches</div>'
        f'<div>Schedule</div><div>Last run</div><div>Last alert</div><div>Status</div></div>'
        f'{rows}</div></section>'
    )


def render_html(mon):
    body = "".join(_group_html(g) for g in mon["groups"])
    navlinks = "".join(
        f'<a href="#g-{g["key"]}" class="gnav hov"><span style="display:flex;align-items:center;gap:8px">'
        f'<span>{esc(g["label"])}</span>'
        + (f'<span class="mono" style="font-size:10.5px;font-weight:500;color:var(--broken-txt);'
           f'background:var(--broken-t);border-radius:20px;padding:0 6px">'
           f'{sum(1 for r in g["rows"] if r.get("status")=="broken")}</span>'
           if any(r.get("status") == "broken" for r in g["rows"]) else "")
        + f'</span><span class="mono" data-navcount="{g["key"]}">{len(g["rows"])}</span></a>'
        for g in mon["groups"] if g["rows"]
    )
    c = mon["counts"]
    total = sum(c.values()) or 1
    data = json.dumps({"counts": c, "groups": [{"key": g["key"], "n": len(g["rows"])} for g in mon["groups"]]})
    return (HTML_TEMPLATE
            .replace("/*__FONTS__*/", inv.read_or_empty(os.path.join(HERE, "fonts.css")))
            .replace("/*__TOKENS__*/", shell.TOKENS)
            .replace("__TOPBAR__", shell.topbar("mon"))
            .replace("__HOST__", config.HOST)
            .replace("__HBADGE__", config.HOST_BADGE)
            .replace("__BADGEJS__", shell.BADGE_JS)
            .replace("__BODY__", body)
            .replace("__GNAV__", navlinks)
            .replace("__GENERATED__", esc(mon["generated_at"]))
            .replace("__N_LIVE__", str(c.get("live", 0)))
            .replace("__N_BROKEN__", str(c.get("broken", 0)))
            .replace("__N_INFO__", str(c.get("info", 0)))
            .replace("__N_TOTAL__", str(len(mon["groups"]) and sum(len(g["rows"]) for g in mon["groups"])))
            .replace("__BAR__", "".join(
                f'<div style="height:100%;width:{c.get(k,0)/total*100}%;background:{STATUS_COL[k]}"></div>'
                for k in ("live", "info", "broken")))
            .replace("/*__DATA__*/", data))


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>__HOST__ — monitor</title>
<style>
/*__FONTS__*/
/*__TOKENS__*/
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);transition:background-color .4s ease}
body{font-family:Figtree,system-ui,sans-serif;-webkit-font-smoothing:antialiased}
html{scroll-behavior:smooth}
a{color:var(--text);text-decoration:none}a:hover{color:var(--text2)}
.mono{font-family:'IBM Plex Mono',monospace}
.hidden{display:none !important}
.hov:hover{background:var(--hover)}
.segbtn{border:0;border-radius:7px;padding:6px 13px;font:inherit;font-size:13px;font-weight:500;cursor:pointer;background:transparent;color:var(--text2);display:flex;align-items:center;gap:7px}
.segbtn.on{background:var(--action);color:var(--on-action)}
.gnav{display:flex;align-items:center;justify-content:space-between;gap:8px;border-radius:8px;padding:6px 12px;font-size:13.5px;font-weight:500;color:var(--text2)}
.grp{margin-top:34px;display:flex;flex-direction:column;gap:10px;scroll-margin-top:140px}
.grphead{display:flex;align-items:baseline;justify-content:space-between;gap:16px;padding:0 2px}
.ghtitle{display:flex;align-items:center;gap:10px}
.ghtitle h2{margin:0;font-size:17px;font-weight:600;letter-spacing:-.005em}
.gbad{font-family:'IBM Plex Mono',monospace;font-size:11px;font-weight:600;color:var(--broken);background:var(--broken-t);border-radius:20px;padding:1px 8px}
.gcount{font-size:12px;color:var(--muted)}
.tbl{background:var(--surface);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);overflow:hidden}
.thead,.row{display:grid;grid-template-columns:22px minmax(190px,1.35fr) minmax(230px,2.1fr) minmax(120px,1fr) minmax(96px,.75fr) minmax(120px,1fr) 84px;gap:0 14px;align-items:start}
.thead{padding:10px 18px;background:var(--surface2);border-bottom:1px solid var(--line);font-size:11px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--faint)}
.row{border-top:1px solid var(--line);padding:11px 18px}
.row:first-of-type{border-top:0}
.row[data-status="broken"]{background:var(--broken-t)}
.cdot{padding-top:4px}
.cdot>span{display:block;width:8px;height:8px;border-radius:50%}
.cname{min-width:0}
.nm{font-size:13.5px;font-weight:600;overflow-wrap:anywhere}
.path{font-size:11px;color:var(--faint);overflow-wrap:anywhere;margin-top:1px}
.cwatch{font-size:13px;color:var(--text2);min-width:0;overflow-wrap:anywhere}
.csch{font-size:12px;color:var(--muted);min-width:0;overflow-wrap:anywhere}
.clast{font-size:12px;color:var(--text2);min-width:0;overflow-wrap:anywhere}
.calert{font-size:12px;color:var(--muted);min-width:0;overflow-wrap:anywhere}
.cstat{padding-top:1px}
.badge{font-family:'IBM Plex Mono',monospace;font-size:10.5px;font-weight:600;border-radius:20px;padding:2px 9px;text-transform:uppercase;letter-spacing:.03em}
@media(max-width:1000px){
 .thead{display:none}
 .row{grid-template-columns:22px minmax(0,1fr);gap:2px 12px}
 .cname{grid-column:2}
 .cwatch,.csch,.clast,.calert{grid-column:2;font-size:12.5px}
 .cwatch:before{content:"watches: ";color:var(--faint)}
 .csch:before{content:"schedule: ";color:var(--faint)}
 .clast:before{content:"last run: ";color:var(--faint)}
 .calert:before{content:"last alert: ";color:var(--faint)}
 .cstat{grid-column:2}
}
@media(max-width:900px){.app{grid-template-columns:1fr !important}.aside{position:static !important;height:auto !important}}
</style></head>
<body>
<div style="display:flex;flex-direction:column;min-height:100vh;background:var(--bg);color:var(--text);font-family:Figtree,system-ui,sans-serif;font-size:14px;line-height:1.5">
__TOPBAR__
<div class="app" style="display:grid;grid-template-columns:272px minmax(0,1fr);flex:1;background:var(--bg);color:var(--text);font-family:Figtree,system-ui,sans-serif;font-size:14px;line-height:1.5">
  <aside class="aside" style="position:sticky;top:56px;height:calc(100vh - 56px);overflow-y:auto;border-right:1px solid var(--line);background:var(--surface2);display:flex;flex-direction:column;gap:22px;padding:26px 16px 22px">
    <div style="display:flex;align-items:center;gap:10px;padding:0 8px">
      <div class="mono" style="flex:none;width:30px;height:30px;border-radius:8px;background:var(--action);color:var(--on-action);display:grid;place-items:center;font-weight:500;font-size:13px">__HBADGE__</div>
      <div style="display:flex;flex-direction:column;line-height:1.3;min-width:0">
        <span style="font-weight:600;font-size:15px;letter-spacing:-.01em">Monitoring</span>
        <span class="mono" style="font-size:11.5px;color:var(--muted)">__HOST__ status</span>
      </div>
    </div>
    <nav id="nav" style="display:flex;flex-direction:column;gap:1px">
      <div class="mono" style="font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--faint);padding:0 12px 8px">On this page</div>
      __GNAV__
    </nav>
    <div style="margin-top:auto;display:flex;flex-direction:column;gap:9px;padding:14px 12px 0;border-top:1px solid var(--line);font-size:12px;color:var(--muted)">
      <div class="mono" style="font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--faint)">Legend</div>
      <div style="display:flex;align-items:center;gap:8px"><span style="flex:none;width:8px;height:8px;border-radius:50%;background:var(--live)"></span>live — ran on schedule</div>
      <div style="display:flex;align-items:center;gap:8px"><span style="flex:none;width:8px;height:8px;border-radius:50%;background:var(--broken)"></span>broken — missed or failing</div>
      <div style="display:flex;align-items:center;gap:8px"><span style="flex:none;width:8px;height:8px;border-radius:50%;background:var(--info)"></span>info — passive check</div>
      <div style="display:flex;align-items:center;gap:8px"><span style="flex:none;width:8px;height:8px;border-radius:50%;background:var(--yellow)"></span>alerted in the last 24h</div>
      <div style="margin-top:6px;font-size:11.5px;line-height:1.45;color:var(--faint)">Read-only. Nothing on this page acts on its own.</div>
    </div>
  </aside>
  <main style="min-width:0;padding:0 40px 80px">
    <header style="display:flex;align-items:flex-end;justify-content:space-between;gap:24px;padding:34px 0 22px">
      <div style="display:flex;flex-direction:column;gap:6px">
        <h1 style="margin:0;font-family:Newsreader,Georgia,serif;font-weight:500;font-size:34px;letter-spacing:-.015em;line-height:1.1">Everything that watches this box</h1>
        <div class="mono" style="font-size:12px;color:var(--muted)">generated __GENERATED__ · __HOST__</div>
      </div>
      <div style="display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted);white-space:nowrap"><span style="width:8px;height:8px;border-radius:50%;background:var(--yellow)"></span><span id="themeLabel"></span></div>
    </header>
    <section style="background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);overflow:hidden">
      <div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr))">
        <div class="chip"><div class="chipl"><span style="width:8px;height:8px;border-radius:50%;background:var(--live)"></span>Live</div><div class="chipn" style="color:var(--text)">__N_LIVE__</div></div>
        <div class="chip"><div class="chipl"><span style="width:8px;height:8px;border-radius:50%;background:var(--broken)"></span>Broken</div><div class="chipn" style="color:var(--broken)">__N_BROKEN__</div></div>
        <div class="chip"><div class="chipl"><span style="width:8px;height:8px;border-radius:50%;background:var(--info)"></span>Info</div><div class="chipn" style="color:var(--text)">__N_INFO__</div></div>
        <div class="chip"><div class="chipl"><span style="width:8px;height:8px;border-radius:2px;background:var(--yellow)"></span>Entries</div><div class="chipn" style="color:var(--text)">__N_TOTAL__</div></div>
      </div>
      <div style="display:flex;height:6px;background:var(--line)">__BAR__</div>
    </section>
    <div style="position:sticky;top:56px;z-index:5;background:var(--bg);padding:18px 0 14px;margin-top:6px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;border-bottom:1px solid var(--line)">
      <div id="statusBtns" style="display:flex;gap:2px;padding:3px;border:1px solid var(--line);border-radius:10px;background:var(--surface)"></div>
      <div style="flex:1;min-width:220px;display:flex;align-items:center;gap:10px;border:1px solid var(--line);border-radius:10px;padding:0 13px;background:var(--surface)"><span class="mono" style="font-size:13px;color:var(--faint)">/</span><input id="q" oninput="onSearch(this.value)" placeholder="filter by name…" style="flex:1;border:0;outline:0;background:transparent;color:var(--text);font-family:'IBM Plex Mono',monospace;font-size:13px;padding:9px 0"></div>
      <div style="font-size:12.5px;color:var(--muted);white-space:nowrap"><span class="mono" id="shown" style="color:var(--text)"></span> shown</div>
    </div>
    <div id="sections">__BODY__</div>
    <footer class="mono" style="margin-top:56px;padding-top:18px;border-top:1px solid var(--line);font-size:11.5px;color:var(--muted)">Generated by monitor.py · read-only · alerts go to Telegram</footer>
  </main>
</div>
</div>
<style>.chip{display:flex;flex-direction:column;gap:4px;padding:18px 22px;border-left:1px solid var(--line)}.chip:first-child{border-left:0}.chipl{display:flex;align-items:center;gap:8px;font-size:12px;font-weight:600;color:var(--muted)}.chipn{font-family:'IBM Plex Mono',monospace;font-size:30px;font-weight:500;letter-spacing:-.02em;line-height:1.15}</style>
<script>
const DATA=/*__DATA__*/;
const COL={live:"var(--live)",broken:"var(--broken)",info:"var(--info)"};
const SUN=[[6.67,17.6],[6.5,17.9],[6.1,18.15],[5.6,18.35],[5.25,18.6],[5.1,18.85],[5.25,18.8],[5.5,18.5],[5.75,18.0],[5.9,17.6],[6.15,17.25],[6.5,17.25]];
let S={filter:"all",q:""};
function localNow(){const d=new Date();return {m:d.getMonth(),h:d.getHours()+d.getMinutes()/60,hh:String(d.getHours()).padStart(2,"0"),mm:String(d.getMinutes()).padStart(2,"0")};}
function isDark(){const n=localNow();const[r,s]=SUN[n.m];return!(n.h>=r&&n.h<s);}
function applyTheme(){document.documentElement.dataset.theme=isDark()?"dark":"light";const n=localNow();document.getElementById("themeLabel").textContent=(isDark()?"Night":"Day")+" theme · local "+n.hh+":"+n.mm;}
function pass(el){const okS=S.filter==="all"||el.dataset.status===S.filter;const okQ=!S.q||el.dataset.name.includes(S.q);return okS&&okQ;}
function setFilter(k){S.filter=k;apply();}
function onSearch(v){S.q=v.toLowerCase();apply();}
function apply(){
  document.querySelectorAll("[data-f]").forEach(b=>b.classList.toggle("on",b.dataset.f===S.filter));
  let shown=0;
  document.querySelectorAll(".grp").forEach(sec=>{let vis=0;const rows=sec.querySelectorAll(".row");
    rows.forEach(r=>{const v=pass(r);r.classList.toggle("hidden",!v);if(v)vis++;});
    sec.classList.toggle("hidden",vis===0&&rows.length>0&&S.filter!=="all");shown+=vis;
    const el=document.querySelector('[data-navcount="'+sec.dataset.grp+'"]');if(el)el.textContent=vis;
  });
  document.getElementById("shown").textContent=shown;
}
function init(){
  applyTheme();setInterval(applyTheme,60000);
  document.getElementById("statusBtns").innerHTML=[["All","all"],["Live","live"],["Broken","broken"],["Info","info"]].map(([l,k])=>`<button class="segbtn" data-f="${k}" onclick="setFilter('${k}')">${k==="all"?"":`<span style="width:7px;height:7px;border-radius:50%;background:${COL[k]}"></span>`}${l}</button>`).join("");
  apply();
}
window.addEventListener("DOMContentLoaded",init);
</script><script>__BADGEJS__</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(OUT_DIR, "index.html"))
    ap.add_argument("--json", action="store_true", help="also dump raw JSON")
    args = ap.parse_args()
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    mon = collect()
    with open(args.out, "w") as fh:
        fh.write(render_html(mon))
    if args.json:
        with open(os.path.splitext(args.out)[0] + ".json", "w") as fh:
            json.dump(mon, fh, indent=1, ensure_ascii=False)
    c = mon["counts"]
    n = sum(len(g["rows"]) for g in mon["groups"])
    print(f"wrote {args.out} — {n} entries: "
          f"{c['live']} live, {c['broken']} broken, {c['info']} info")


if __name__ == "__main__":
    main()
