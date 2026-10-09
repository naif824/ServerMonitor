#!/usr/bin/env python3
"""ServerMonitor inventory pane — one read-only view of every component on a box.

Enumerates containers, docker objects, systemd units (all states), every cron
(user + /etc/crontab + /etc/cron.d + periodic dirs), nginx vhosts (enabled vs
orphaned), pm2 processes and system health, classifies each by its TRUE state,
and renders a single self-contained HTML page.

Design rules:
  - READ ONLY. This tool never starts, stops, restarts or deletes anything.
    It observes and reports; every cleanup stays a human decision.
  - Three states, not two:  live (green) / dormant (grey, off on purpose) /
    broken (red, actually wrong). Plus an "abandoned" flag for leftovers.
  - No new daemon: it is a generator you run from cron and point nginx at.
  - Config-driven: host name, thresholds and heartbeats come from config.py.
  - Degrades gracefully: if docker / systemctl / nginx / certbot / pm2 / apt are
    absent, that category is simply empty instead of crashing.

Usage:
  python3 inventory.py                 # writes index.html next to this file
  python3 inventory.py --out /tmp/x.html --json
"""
import argparse, json, os, re, shutil, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import config
import shell

HERE = os.path.dirname(os.path.abspath(__file__))
NGINX_AVAIL = "/etc/nginx/sites-available"
NGINX_ENABLED = "/etc/nginx/sites-enabled"
SYSTEMD_LOCAL = "/etc/systemd/system"
BIND_ZONES = "/etc/bind/zones"
CRON_D = "/etc/cron.d"
CRONTAB_SYSTEM = "/etc/crontab"
CRON_PERIODIC = ["/etc/cron.hourly", "/etc/cron.daily",
                 "/etc/cron.weekly", "/etc/cron.monthly"]

# Abandonment thresholds (seconds)
OLD_CONTAINER = 7 * 86400
OLD_VHOST_BAK = True          # any *.bak/.seed-backup/.pre-* counts as leftover


# ---------------------------------------------------------------- shell helper
def sh(cmd, timeout=60):
    """Run a command list, return stdout ('' on any failure). Never raises."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:
        return ""


def age_from_seconds(sec):
    sec = int(sec)
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h"
    return f"{sec // 86400}d"


def human_age(created_epoch):
    if not created_epoch:
        return ""
    return age_from_seconds(time.time() - created_epoch) + " old"


# ---------------------------------------------------------------- items
def item(cat, name, status, detail="", abandoned=False, reason="", **meta):
    meta.setdefault("scope", "local")   # local = this host's; vendor = OS plumbing
    return {"cat": cat, "name": name, "status": status, "detail": detail,
            "abandoned": bool(abandoned), "reason": reason, "meta": meta}


# ---------------------------------------------------------------- containers
def _compose_managed(c):
    """Is this container still defined in its docker-compose file?

    A stopped container that compose still defines is intentionally stopped
    (or pending a redeploy) — NOT an orphan to delete. Deleting it would only
    be undone by the next `compose up`.
    """
    labels = dict(p.split("=", 1) for p in (c.get("Labels") or "").split(",") if "=" in p)
    cfg = labels.get("com.docker.compose.project.config_files", "")
    if not cfg or not os.path.isfile(cfg):
        return False
    try:
        return c.get("Names", "") in open(cfg).read()
    except Exception:
        return False


def discover_containers():
    out = []
    raw = sh(["docker", "ps", "-a", "--format", "{{json .}}"])
    for line in raw.splitlines():
        try:
            c = json.loads(line)
        except Exception:
            continue
        name = c.get("Names", "?")
        state = (c.get("State") or "").lower()
        status_txt = c.get("Status", "")
        image = c.get("Image", "")
        ports = c.get("Ports", "")
        nets = c.get("Networks", "")
        code = None
        m = re.search(r"\((\d+)\)", status_txt)
        if m:
            code = int(m.group(1))
        managed = _compose_managed(c)
        abandoned, reason = False, ""
        if state == "running":
            status = "live"
        elif state in ("created", "dead") or (state == "exited" and code not in (0, 130, 143)):
            status = "broken"                      # crashed or never started
            if not managed:
                abandoned, reason = True, "orphan container (not in any compose file)"
        elif state == "exited":
            status = "dormant"                     # stopped cleanly / on purpose
            if not managed:
                abandoned, reason = True, "orphan container (not in any compose file)"
        else:  # paused, restarting
            status = "info"
        detail = " · ".join(x for x in [status_txt, image, nets, ports] if x)
        out.append(item("containers", name, status, detail,
                        abandoned, reason, image=image, state=state, managed=managed))
    return out


# ---------------------------------------------------------------- docker objects
def discover_images():
    out = []
    used = set(sh(["docker", "ps", "-a", "--format", "{{.Image}}"]).splitlines())
    # dangling (untagged, unreferenced) images are NOT listed by `docker images`
    # --format, so pull them explicitly or the pane silently hides reclaimable ones
    seen_ids = set()
    for iid in sh(["docker", "images", "-qf", "dangling=true"]).split():
        seen_ids.add(iid)
        out.append(item("images", f"<dangling> {iid[:12]}", "dormant",
                        "untagged, unreferenced (reclaimable)", True,
                        "dangling image"))
    raw = sh(["docker", "images", "--format", "{{json .}}"])
    for line in raw.splitlines():
        try:
            im = json.loads(line)
        except Exception:
            continue
        repo, tag = im.get("Repository", "?"), im.get("Tag", "")
        name = f"{repo}:{tag}"
        dangling = repo == "<none>" or tag == "<none>"
        in_use = name in used or repo in used
        if dangling:
            out.append(item("images", name, "dormant",
                            f"dangling · {im.get('Size','')}",
                            True, "untagged, unreferenced image"))
        elif not in_use:
            out.append(item("images", name, "dormant",
                            f"unused · {im.get('Size','')} · {im.get('CreatedSince','')}",
                            True, "no container uses this image"))
        else:
            out.append(item("images", name, "live",
                            f"{im.get('Size','')} · {im.get('CreatedSince','')}"))
    return out


def discover_volumes():
    out = []
    dangling = set(sh(["docker", "volume", "ls", "-qf", "dangling=true"]).split())
    raw = sh(["docker", "volume", "ls", "--format", "{{json .}}"])
    for line in raw.splitlines():
        try:
            v = json.loads(line)
        except Exception:
            continue
        name = v.get("Name", "?")
        if name in dangling:
            out.append(item("volumes", name, "dormant",
                            f"unused · {v.get('Driver','')}",
                            True, "no container references it"))
        else:
            out.append(item("volumes", name, "live",
                            f"in use · {v.get('Driver','')}"))
    return out


def discover_networks():
    out = []
    raw = sh(["docker", "network", "ls", "--format", "{{json .}}"])
    builtin = {"bridge", "host", "none"}
    for line in raw.splitlines():
        try:
            n = json.loads(line)
        except Exception:
            continue
        name = n.get("Name", "?")
        if name in builtin:
            out.append(item("networks", name, "info", f"{n.get('Driver','')} (default)"))
            continue
        # count attached containers via inspect
        insp = sh(["docker", "network", "inspect", name, "--format",
                   "{{len .Containers}}"])
        cnt = insp.strip() or "0"
        if cnt == "0":
            out.append(item("networks", name, "dormant",
                            f"{n.get('Driver','')} · 0 containers",
                            True, "no containers attached"))
        else:
            out.append(item("networks", name, "live",
                            f"{n.get('Driver','')} · {cnt} containers"))
    return out


# ---------------------------------------------------------------- systemd
_UNIT_STATES = None


def _unit_states():
    """Cached: services/timers/sockets all share one pair of systemctl reads."""
    global _UNIT_STATES
    if _UNIT_STATES is not None:
        return _UNIT_STATES
    files, active = {}, {}
    for line in sh(["systemctl", "list-unit-files", "--no-pager",
                    "--no-legend"]).splitlines():
        p = line.split()
        if len(p) >= 2:
            files[p[0]] = p[1]
    for line in sh(["systemctl", "list-units", "--all", "--no-pager",
                    "--no-legend", "--plain"]).splitlines():
        p = line.split(None, 4)
        if len(p) >= 4:
            active[p[0]] = (p[2], p[3], p[4] if len(p) > 4 else "")
    _UNIT_STATES = (files, active)
    return _UNIT_STATES


_ACTIVATED = None


def _activated_services():
    """Service units started by a timer, path or socket.

    Such a service is HEALTHY while 'inactive' between runs, so it must never
    be read as abandoned. Built from active timers plus every local .timer/
    .path/.socket unit (by its Unit= or its same-named service), because a
    condition-gated or path-triggered unit will not appear in list-timers.
    """
    global _ACTIVATED
    if _ACTIVATED is not None:
        return _ACTIVATED
    acts = set()
    # Active timers only (no --all): an inactive/disabled timer schedules nothing.
    for line in sh(["systemctl", "list-timers", "--no-pager",
                    "--no-legend"]).splitlines():
        parts = line.split()
        if parts and parts[-1].endswith(".service"):
            acts.add(parts[-1])
    try:
        act_files = [f for f in os.listdir(SYSTEMD_LOCAL)
                     if f.endswith((".timer", ".path", ".socket"))]

        def live(f):
            en = sh(["systemctl", "is-enabled", f]).strip()
            return en in ("enabled", "enabled-runtime") or \
                sh(["systemctl", "is-active", f]).strip() == "active"

        with ThreadPoolExecutor(max_workers=8) as ex:
            for f, ok in zip(act_files, ex.map(live, act_files)):
                if not ok:
                    continue
                try:
                    text = open(os.path.join(SYSTEMD_LOCAL, f)).read()
                except Exception:
                    text = ""
                m = re.search(r"^\s*Unit\s*=\s*(\S+)", text, re.M)
                acts.add(m.group(1) if m else f.rsplit(".", 1)[0] + ".service")
    except Exception:
        pass
    _ACTIVATED = acts
    return acts


def _unit_type(unit):
    return sh(["systemctl", "show", "-p", "Type", "--value", unit]).strip()


def discover_units(kind, suffix):
    """kind: 'services' or 'timers'. suffix: '.service' / '.timer'."""
    files, active = _unit_states()
    activated = _activated_services()
    out = []
    seen = set()
    for unit, fstate in files.items():
        if not unit.endswith(suffix):
            continue
        stem = unit[:-len(suffix)]
        if stem.endswith("@"):
            continue
        if stem in seen:
            continue
        seen.add(stem)
        local = os.path.isfile(os.path.join(SYSTEMD_LOCAL, unit))
        act, sub, desc = active.get(unit, ("inactive", "", ""))
        scope = "local" if local else "vendor"
        abandoned, reason = False, ""

        if act == "failed":
            status = "broken"
        elif act == "active":
            status = "live"
        elif unit in activated:
            # timer-driven oneshot, idle between runs — healthy, not abandoned
            status = "live"
            desc = ("timer-scheduled; idle now" if not desc else desc[:50] + " (timer)")
        elif fstate == "enabled" and act == "inactive" and kind == "services":
            # enabled but not running. Red ONLY for the operator's own long-running unit
            # that nothing justifies being down: vendor units, oneshots/idle and
            # condition-skipped units (e.g. open-vm-tools on a non-VM box) are
            # normal, not broken.
            cond = sh(["systemctl", "show", "-p", "ConditionResult",
                       "--value", unit]).strip()
            btype = _unit_type(unit)
            if not local or cond == "no" or btype in ("oneshot", "idle"):
                status = "dormant"
            else:
                status = "broken"
                reason = "enabled but not running"
        else:
            status = "dormant"
            # A locally-owned unit that nothing runs is a cleanup candidate.
            # Static units are exempt only if something (timer/path/socket)
            # activates them — those are handled as 'live' above.
            if local and fstate in ("disabled", "masked"):
                abandoned, reason = True, f"local unit {fstate} + inactive"
            elif local and fstate == "static" and unit not in activated:
                abandoned, reason = True, "local unit defined but nothing activates it"

        d = " · ".join(x for x in [f"file={fstate}", f"active={act}",
                                   scope, desc[:60]] if x)
        out.append(item(kind, stem, status, d, abandoned, reason,
                        file_state=fstate, active=act, local=local, scope=scope))
    return out


# ---------------------------------------------------------------- crons
CRON_LINE = re.compile(
    r"^\s*(@\w+|[-\d*/,]+\s+[-\d*/,]+\s+[-\d*/,]+\s+[-\d*/,]+\s+[-\d*/,]+)\s+(.*)$")


def _cron_entries():
    """Yield (source, schedule, command, line_no) for every cron anywhere."""
    # user crontab
    for i, ln in enumerate(sh(["crontab", "-l"]).splitlines(), 1):
        if ln.strip() and not ln.strip().startswith("#"):
            m = CRON_LINE.match(ln)
            if m:
                yield ("user crontab", m.group(1), m.group(2), i)
    # /etc/crontab + /etc/cron.d/*
    paths = [CRONTAB_SYSTEM]
    if os.path.isdir(CRON_D):
        paths += [os.path.join(CRON_D, f) for f in sorted(os.listdir(CRON_D))]
    for path in paths:
        try:
            with open(path) as fh:
                for i, ln in enumerate(fh, 1):
                    s = ln.strip()
                    if not s or s.startswith("#"):
                        continue
                    if len(s.split()) == 1 and "=" in s:      # env assignment, e.g. SHELL=/bin/sh
                        continue
                    m = CRON_LINE.match(ln)
                    if m:
                        yield (path, m.group(1), m.group(2), i)
                    # /etc/cron.d / /etc/crontab lines carry a user field: skip strict
        except Exception:
            continue
    # periodic dirs (scripts run by run-parts)
    for d in CRON_PERIODIC:
        if os.path.isdir(d):
            for f in sorted(os.listdir(d)):
                if f.startswith("."):
                    continue
                p = os.path.join(d, f)
                if os.path.isfile(p):
                    yield (d + "/" + f, d.rsplit("/", 1)[1].replace("cron.", "@"),
                           p, 0)


def _missing_target(cmd):
    """Conservatively find a cron's script when we are CONFIDENT it is gone.

    Returns a path only for an absolute path that does not exist, or a relative
    path resolved against the command's own 'cd' directory. Commands that run
    inside a container (docker exec/run) and bare relative paths with no known
    working dir are left unverified rather than falsely marked broken — a
    trustworthy red matters more than catching every miss.
    """
    if re.search(r"\bdocker(\s+compose)?\s+(exec|run)\b", cmd):
        return None
    wd = None
    m = re.search(r"\bcd\s+([^\s;&|]+)", cmd)
    if m:
        wd = m.group(1)
    for path in re.findall(r"[\w./@-]+\.(?:sh|py|js|php|rb|pl)\b", cmd):
        if path.startswith("/"):
            if os.path.exists(path):
                continue
            if wd and os.path.exists(os.path.join(wd, os.path.basename(path))):
                continue
            return path
        if wd:
            full = os.path.join(wd, path)
            if not os.path.exists(full):
                return full
    return None


def discover_crons():
    out = []
    seen_cmds = {}
    for source, sched, cmd, _ln in _cron_entries():
        cmd_core = re.sub(r"\s*&&\s*curl -fsS.*$", "", cmd).strip()  # strip kuma ping
        label = cmd_core.split()[0] if cmd_core.split() else "job"
        label = re.sub(r"^.*/", "", label)
        missing = _missing_target(cmd_core)
        if missing:
            status, abandoned, reason = "broken", True, f"target missing: {missing}"
        else:
            status, abandoned, reason = "info", False, ""
        m = re.search(r"([\w.-]+\.(?:sh|py|js|php|rb|pl))", cmd_core)
        if m:
            label = m.group(1)
        name = f"{label}  [{os.path.basename(source)}]"
        # Only the SAME command at the SAME schedule is a real duplicate;
        # a command run both @reboot and */5 is two intended schedules.
        seen_cmds.setdefault((sched, cmd_core), []).append(source)
        scope = "local" if source == "user crontab" else "vendor"
        out.append(item("crons", name, status, f"{sched} · {source}",
                        abandoned, reason, source=source, schedule=sched, scope=scope))
    # duplicate detection — same command AND same schedule
    for (sched, cmd_core), srcs in seen_cmds.items():
        if len(srcs) > 1:
            out.append(item("crons", f"DUPLICATE: {cmd_core[:60]}", "broken",
                            f"{sched} · appears in: " + ", ".join(srcs), False,
                            "same command at the same schedule scheduled more than once"))
    return out


# ---------------------------------------------------------------- nginx
def discover_nginx():
    out = []
    avail = sorted(os.listdir(NGINX_AVAIL)) if os.path.isdir(NGINX_AVAIL) else []
    en = sorted(os.listdir(NGINX_ENABLED)) if os.path.isdir(NGINX_ENABLED) else []
    en_set = set(en)
    for name in avail:
        p = os.path.join(NGINX_AVAIL, name)
        is_bak = bool(re.search(r"\.bak|\.seed-backup|\.pre-|\.before-|\.shutdown", name))
        enabled = name in en_set
        if is_bak:
            out.append(item("nginx", name, "dormant", "backup file in sites-available",
                            True, "looks like a rotated backup"))
        elif not enabled:
            out.append(item("nginx", name, "dormant",
                            "available but NOT enabled (orphaned)",
                            True, "no symlink in sites-enabled"))
        else:
            out.append(item("nginx", name, "live", "enabled and served"))
    # enabled entries without a matching available file / broken symlinks
    for name in en:
        p = os.path.join(NGINX_ENABLED, name)
        if not os.path.exists(p):
            out.append(item("nginx", name, "broken", "enabled but target missing"))
        elif name not in set(avail):
            out.append(item("nginx", name, "live", "enabled (no sites-available copy)"))
    return out


# ---------------------------------------------------------------- pm2
def discover_pm2():
    out = []
    try:
        data = json.loads(sh(["pm2", "jlist"]) or "[]")
    except Exception:
        return out
    for p in data:
        env = p.get("pm2_env", {})
        name = p.get("name", "?")
        st = env.get("status", "?")
        status = {"online": "live", "stopped": "dormant",
                  "errored": "broken"}.get(st, "info")
        restarts = env.get("restart_time", 0)
        detail = f"status={st} · restarts={restarts}"
        abandoned = st == "stopped"
        out.append(item("pm2", name, status, detail, abandoned,
                        "stopped process" if abandoned else ""))
    return out


# ---------------------------------------------------------------- system health
def discover_system(thresholds=None):
    t = thresholds or {"disk_pct": config.threshold("disk_pct", 90),
                       "mem_pct": config.threshold("mem_pct", 90),
                       "load_per_core": config.threshold("load_per_core", 2.0)}
    out = []
    du = shutil.disk_usage("/")
    dp = du.used / du.total * 100
    out.append(item("system", "disk /", "broken" if dp >= t["disk_pct"] else "live",
                    f"{dp:.0f}% used of {du.total/1e9:.0f}GB"))
    mem = {}
    try:
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            mem[k] = int(v.strip().split()[0])
        mp = (mem["MemTotal"] - mem["MemAvailable"]) / mem["MemTotal"] * 100
        out.append(item("system", "memory",
                        "broken" if mp >= t["mem_pct"] else "live",
                        f"{mp:.0f}% used of {mem['MemTotal']/1e6:.0f}GB"))
    except Exception:
        pass
    cores = os.cpu_count() or 1
    lp = os.getloadavg()[1] / cores
    out.append(item("system", "load",
                    "broken" if lp >= t["load_per_core"] else "live",
                    f"{os.getloadavg()[1]:.2f} on {cores} cores"))
    out.append(item("system", "uptime", "info",
                    age_from_seconds(time.time() - float(open("/proc/uptime").read().split()[0]))))
    return out


# ---------------------------------------------------------------- build
def discover_sockets():
    """systemd socket / path / mount units (the activators behind services)."""
    files, active = _unit_states()
    out = []
    for unit, fstate in files.items():
        if not unit.endswith((".socket", ".path", ".mount")):
            continue
        act, sub, desc = active.get(unit, ("inactive", "", ""))
        local = os.path.isfile(os.path.join(SYSTEMD_LOCAL, unit))
        status = "broken" if act == "failed" else "live" if act == "active" else "dormant"
        d = " · ".join(x for x in [f"file={fstate}", f"active={act}",
                                   ("local" if local else "vendor"), desc[:50]] if x)
        out.append(item("sockets", unit, status, d, False, "",
                        kind=unit.rsplit(".", 1)[1],
                        scope="local" if local else "vendor"))
    return out


def discover_packages():
    """Manually-installed OS packages only (apt-mark showmanual)."""
    out = []
    for n in sh(["apt-mark", "showmanual"]).split():
        out.append(item("packages", n, "info", "manually installed",
                        False, "", scope="vendor"))
    return out


def discover_users():
    """Login-capable accounts + root, with sudo membership."""
    out = []
    sudoers = set()
    try:
        for line in open("/etc/group"):
            p = line.split(":")
            if p[0] in ("sudo", "wheel", "admin") and len(p) > 3:
                sudoers.update(x for x in p[3].strip().split(",") if x)
    except Exception:
        pass
    try:
        for line in open("/etc/passwd"):
            f = line.split(":")
            if len(f) < 7 or not f[2].isdigit():
                continue
            name, uid, shell = f[0], int(f[2]), f[6]
            if uid != 0 and uid < 1000:
                continue
            login = shell not in ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false")
            d = f"uid={uid} · {shell}" + (f" · {f[4]}" if f[4] else "") + \
                (" · sudo" if name in sudoers else "")
            out.append(item("users", name, "live" if login else "info", d,
                            False, "", scope="local" if login else "vendor"))
    except Exception:
        pass
    return out


def discover_heartbeats():
    """Dead-man's-switch: flag any critical cron job whose last success is stale."""
    out = []
    try:
        hb = config.heartbeats()
    except Exception:
        return out
    d = os.path.join(config.STATE, "hb")
    now = time.time()
    for name, meta in hb.items():
        maxmin = meta.get("max_age_min", 120)
        desc = meta.get("desc", "")
        p = os.path.join(d, name)
        if not os.path.exists(p):
            out.append(item("heartbeats", name, "info", f"awaiting first run · {desc}"))
            continue
        try:
            last = float(open(p).read().strip())
        except Exception:
            last = os.path.getmtime(p)
        age_min = (now - last) / 60
        if age_min > maxmin:
            out.append(item("heartbeats", name, "broken",
                            f"STALE {age_min/60:.1f}h (max {maxmin/60:.1f}h) · {desc}",
                            False, f"last success {age_min/60:.1f}h ago"))
        else:
            out.append(item("heartbeats", name, "live",
                            f"ok · {age_min:.0f}m ago · {desc}"))
    return out


def _enabled_vhost_text():
    txt = ""
    d = "/etc/nginx/sites-enabled"
    if os.path.isdir(d):
        for f in os.listdir(d):
            txt += read_or_empty(os.path.join(d, f))
    return txt


def read_or_empty(p):
    try:
        return open(p, errors="ignore").read()
    except Exception:
        return ""


SYSTEM_PORTS = {"22": "ssh", "25": "smtp", "53": "dns (bind)", "80": "nginx",
                "443": "nginx", "953": "rndc", "631": "cups", "111": "rpcbind",
                "323": "chrony", "5353": "mdns", "51820": "wireguard"}


def _unit_of_pid(pid):
    try:
        cg = open(f"/proc/{pid}/cgroup").read()
        m = re.search(r"/([\w@.-]+\.service)", cg)
        if m:
            return m.group(1)[:-len(".service")]
    except Exception:
        pass
    return None


def _short(path):
    if not path:
        return "-"
    p = path.replace(os.path.expanduser("~") + "/", "~/")
    return p if len(p) <= 42 else "…" + p[-40:]


def _owner_maps():
    """Auto-derive each port's owner from live sources only.

    Owner = the container publishing it, the systemd unit in the pid's cgroup,
    the pm2 app (via its process tree), or the process — plus the project
    folder and the nginx domain that proxies to it. No file to maintain.
    """
    pm2 = {}
    try:
        for p in json.loads(sh(["pm2", "jlist"]) or "[]"):
            if p.get("pid"):
                cwd = (p.get("pm2_env") or {}).get("pm_cwd", "")
                stack = [str(p["pid"])]
                while stack:
                    cur = stack.pop()
                    pm2.setdefault(cur, {"name": p.get("name", "?"), "dir": cwd})
                    try:
                        kids = open(f"/proc/{cur}/task/{cur}/children").read().split()
                    except Exception:
                        kids = []
                    stack.extend(kids)
    except Exception:
        pass
    dock = {}
    for line in sh(["docker", "ps", "--format",
                    '{{.Names}}|{{.Ports}}|{{.Label "com.docker.compose.project.working_dir"}}|{{.Label "com.docker.compose.project"}}']).splitlines():
        parts = line.split("|")
        if len(parts) < 2:
            continue
        name, ports = parts[0], parts[1]
        wd = parts[2] if len(parts) > 2 else ""
        proj = parts[3] if len(parts) > 3 else ""
        if not wd and proj:
            wd = f"(compose:{proj})"
        for m in re.finditer(r"[:.](\d+)->", ports):
            dock.setdefault(m.group(1), {"name": name, "dir": wd})
    dom = {}
    d = "/etc/nginx/sites-enabled"
    if os.path.isdir(d):
        for f in os.listdir(d):
            txt = read_or_empty(os.path.join(d, f))
            names = [n for grp in re.findall(r"server_name\s+([^;]+);", txt)
                     for n in grp.split()]
            for pm in re.finditer(r"proxy_pass\s+https?://[^;]*?:(\d+)", txt):
                dom.setdefault(pm.group(1), set()).update(names)
    return pm2, dock, dom


def _owner_folder(port, pid, proc, dock, pm2):
    """Return (owner, project_folder) for a listening port."""
    if port in dock:
        return f"container:{dock[port]['name']}", dock[port]["dir"]
    folder = ""
    if pid:
        try:
            folder = os.readlink(f"/proc/{pid}/cwd")
        except Exception:
            pass
        try:
            cl = open(f"/proc/{pid}/cmdline", "rb").read().decode("utf-8", "ignore")
            if "cursor-server" in cl:
                return "cursor-remote (IDE)", folder
        except Exception:
            pass
        u = _unit_of_pid(pid)
        if u:
            return f"systemd:{u}", folder
        if pid in pm2:
            return f"pm2:{pm2[pid]['name']}", (pm2[pid]["dir"] or folder)
    return proc, folder
SYSTEM_PROCS = ("sshd", "ssh", "nginx", "named", "systemd-resolve", "master",
                "postfix", "tailscaled", "syncthing", "containerd", "dockerd",
                "docker-proxy", "imunify", "rpcbind", "chrony", "cups", "dbus",
                "cron", "polkit", "cursor", "tunnel")


def _proc_comm(pid, fallback):
    if pid:
        try:
            cl = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\x00")
            if cl and cl[0]:
                return os.path.basename(cl[0].decode("utf-8", "ignore"))[:30]
        except Exception:
            pass
        try:
            return open(f"/proc/{pid}/comm").read().strip()
        except Exception:
            pass
    return fallback


def discover_ports():
    """Live source of truth: every listening socket, its owner and its folder.

    Ownership is derived from live state only (container / systemd unit / pm2
    app / process, plus the owning project folder and proxy domain) — nothing
    is stored, nothing to forget. ports.json is not used.
    """
    pm2map, dock, dom = _owner_maps()
    listens = {}
    for proto, flag in (("tcp", "-ltnp"), ("udp", "-lunp")):
        for line in sh(["sudo", "-n", "ss", "-H", flag]).splitlines():
            p = line.split()
            if len(p) < 5:
                continue
            m = re.search(r":(\d+)$", p[3])
            if not m:
                continue
            port = m.group(1)
            pm = re.search(r'\("([^"]+)",pid=(\d+)', " ".join(p[5:]))
            pid = pm.group(2) if pm else None
            name = _proc_comm(pid, pm.group(1)) if pm else "-"
            rec = listens.setdefault(port, {"proto": proto, "procs": set(), "pid": None})
            rec["procs"].add(name)
            if pid and not rec["pid"]:
                rec["pid"] = pid
    out = []
    for port in sorted(listens, key=int):
        info = listens[port]
        proc = ",".join(sorted(info["procs"]))[:24]
        owner, folder = _owner_folder(port, info["pid"], proc, dock, pm2map)
        domains = ", ".join(sorted(dom.get(port, []))[:2])
        system = port in SYSTEM_PORTS or any(s in owner for s in SYSTEM_PROCS)
        detail = f"{info['proto'].upper()} :{port} · {owner} · {_short(folder)}"
        if domains:
            detail += f" · {domains}"
        out.append(item("ports", f":{port}  {owner}", "info" if system else "live",
                        detail, False, "", port=port, owner=owner, folder=folder))
    return out


_CERT_CACHE = os.path.join(config.STATE, "certbot.cache")


def _certbot_text():
    """certbot certificates is slow (~0.6s, sudo); certs change ~daily, so cache 6h."""
    try:
        if os.path.exists(_CERT_CACHE) and time.time() - os.path.getmtime(_CERT_CACHE) < 6 * 3600:
            return open(_CERT_CACHE).read()
    except Exception:
        pass
    txt = sh(["sudo", "-n", "certbot", "certificates"], timeout=60)
    try:
        os.makedirs(os.path.dirname(_CERT_CACHE), exist_ok=True)
        open(_CERT_CACHE, "w").write(txt)
    except Exception:
        pass
    return txt


def discover_certs():
    txt = _certbot_text()
    vhosts = _enabled_vhost_text()
    cert_days = config.threshold("cert_days", 7)
    blocks = txt.split("Certificate Name:")
    out = []
    for b in blocks[1:]:
        name = b.splitlines()[0].strip()
        dm = re.search(r"Domains:\s*(.+)", b)
        domains = dm.group(1).strip() if dm else ""
        em = re.search(r"Expiry Date:\s*([\d-]+ [\d:]+)\S*\s*\(VALID:\s*(\d+) day", b)
        days = int(em.group(2)) if em else None
        referenced = f"live/{name}/" in vhosts
        if not referenced:
            status, ab, reason = "dormant", True, "not referenced by any enabled vhost"
        elif days is not None and days < cert_days:
            status, ab, reason = "broken", False, f"expires in {days} days"
        else:
            status, ab, reason = "live", False, ""
        detail = (f"expires in {days}d · " if days is not None else "") + domains[:70]
        out.append(item("certs", name, status, detail, ab, reason, days=days,
                        referenced=referenced))
    return out


def discover_dns():
    if not os.path.isdir(BIND_ZONES):
        return []
    zones = []
    for f in sorted(os.listdir(BIND_ZONES)):
        path = os.path.join(BIND_ZONES, f)
        if not f.startswith("db.") or not os.path.isfile(path):
            continue
        if re.search(r"bak|retired|backup|/", f):
            continue
        body = read_or_empty(path)
        if "SOA" not in body:
            continue
        zones.append((f[3:], path, body))
    # named-checkzone per zone is the slow part -> run them in parallel
    with ThreadPoolExecutor(max_workers=8) as ex:
        checks = list(ex.map(
            lambda z: "OK" in sh(["named-checkzone", z[0], z[1]], timeout=20), zones))
    out = []
    for (zone, path, body), ok in zip(zones, checks):
        sm = re.search(r"([0-9]{8,})\s*;\s*[Ss]erial", body) or \
            re.search(r"SOA.*?\(\s*([0-9]+)", body, re.S)
        serial = sm.group(1) if sm else "?"
        nrec = len(re.findall(r"^\S+\s+IN\s+(?:A|AAAA|CNAME|MX|TXT|NS|SRV|PTR)\b",
                              body, re.M))
        out.append(item("dns", zone, "live" if ok else "broken",
                        f"serial {serial} · {nrec} records" + ("" if ok else " · INVALID"),
                        False, "" if ok else "named-checkzone failed", serial=serial))
    return out


CATS = [
    ("heartbeats", "Heartbeats", discover_heartbeats),
    ("containers", "Containers", discover_containers),
    ("services", "Systemd services", lambda: discover_units("services", ".service")),
    ("timers", "Systemd timers", lambda: discover_units("timers", ".timer")),
    ("crons", "Cron jobs", discover_crons),
    ("nginx", "Nginx vhosts", discover_nginx),
    ("ports", "Ports (registry)", discover_ports),
    ("certs", "Certificates", discover_certs),
    ("dns", "DNS zones", discover_dns),
    ("sockets", "Sockets / paths", discover_sockets),
    ("pm2", "PM2 processes", discover_pm2),
    ("packages", "Packages", discover_packages),
    ("users", "Users", discover_users),
    ("volumes", "Docker volumes", discover_volumes),
    ("networks", "Docker networks", discover_networks),
    ("images", "Docker images", discover_images),
    ("system", "System health", discover_system),
]


def build_inventory():
    cats = []
    counts = {"live": 0, "dormant": 0, "broken": 0, "info": 0}
    abandoned = 0
    for key, label, fn in CATS:
        try:
            items = fn()
        except Exception as e:
            items = [item(key, "(discovery error)", "broken", str(e))]
        for it in items:
            counts[it["status"]] = counts.get(it["status"], 0) + 1
            if it["abandoned"]:
                abandoned += 1
        cats.append({"key": key, "label": label, "items": items})
    return {
        "host": config.HOST,
        "generated_at": datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z"),
        "counts": counts,
        "abandoned": abandoned,
        "categories": cats,
    }


# ---------------------------------------------------------------- render
def render_html(inv):
    data = json.dumps(inv, ensure_ascii=False)
    return (HTML_TEMPLATE.replace("/*__DATA__*/", data)
            .replace("/*__FONTS__*/", read_or_empty(os.path.join(HERE, "fonts.css")))
            .replace("/*__TOKENS__*/", shell.TOKENS)
            .replace("__TOPBAR__", shell.topbar("inv"))
            .replace("__HOST__", config.HOST)
            .replace("__HBADGE__", config.HOST_BADGE)
            .replace("__BADGEJS__", shell.BADGE_JS))


HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__HOST__ — server inventory</title>
<style>
/*__FONTS__*/
/*__TOKENS__*/
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);transition:background-color .4s ease}
body{font-family:Figtree,system-ui,sans-serif;-webkit-font-smoothing:antialiased}
a{color:var(--text);text-decoration:none}a:hover{color:var(--text2)}
input::placeholder{color:var(--faint)}::selection{background:var(--amber-t)}
.mono{font-family:'IBM Plex Mono',monospace}
.hidden{display:none !important}
.hov:hover{background:var(--hover)}
.segbtn{border:0;border-radius:7px;padding:6px 13px;font:inherit;font-size:13px;font-weight:500;cursor:pointer;background:transparent;color:var(--text2);display:flex;align-items:center;gap:7px}
.segbtn.on{background:var(--action);color:var(--on-action)}
.navbtn{border:0;border-radius:8px;padding:6px 12px;font:inherit;font-size:13.5px;cursor:pointer;background:transparent;color:var(--text2);display:flex;align-items:center;justify-content:space-between;gap:8px;width:100%;text-align:left}
.navbtn.on{background:var(--action);color:var(--on-action);font-weight:600}
.navbtn:hover{background:var(--hover)}
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
        <span style="font-weight:600;font-size:15px;letter-spacing:-.01em">Server inventory</span>
        <span class="mono" style="font-size:11.5px;color:var(--muted)">__HOST__ status</span>
      </div>
    </div>
    <nav id="nav" style="display:flex;flex-direction:column;gap:1px"></nav>
    <div style="margin-top:auto;display:flex;flex-direction:column;gap:9px;padding:14px 12px 0;border-top:1px solid var(--line);font-size:12px;color:var(--muted)">
      <div class="mono" style="font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--faint)">Legend</div>
      <div style="display:flex;align-items:center;gap:8px"><span style="flex:none;width:8px;height:8px;border-radius:50%;background:var(--live)"></span>live — running and in use</div>
      <div style="display:flex;align-items:center;gap:8px"><span style="flex:none;width:8px;height:8px;border-radius:50%;background:var(--dormant)"></span>dormant — present, not running</div>
      <div style="display:flex;align-items:center;gap:8px"><span style="flex:none;width:8px;height:8px;border-radius:50%;background:var(--broken)"></span>broken — needs attention</div>
      <div style="display:flex;align-items:center;gap:8px"><span style="flex:none;width:8px;height:8px;border-radius:50%;background:var(--info)"></span>info — inventory only</div>
      <div style="display:flex;align-items:center;gap:8px"><span style="flex:none;width:8px;height:8px;border-radius:2px;background:var(--yellow)"></span>abandoned — safe-to-review</div>
      <div style="margin-top:6px;font-size:11.5px;line-height:1.45;color:var(--faint)">Read-only. Nothing on this page acts on its own.</div>
    </div>
  </aside>
  <main style="min-width:0;padding:0 40px 80px">
    <header style="display:flex;align-items:flex-end;justify-content:space-between;gap:24px;padding:34px 0 22px">
      <div style="display:flex;flex-direction:column;gap:6px">
        <h1 style="margin:0;font-family:Newsreader,Georgia,serif;font-weight:500;font-size:34px;letter-spacing:-.015em;line-height:1.1">Every component on this box</h1>
        <div class="mono" id="gen" style="font-size:12px;color:var(--muted)"></div>
      </div>
      <div style="display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted);white-space:nowrap"><span style="width:8px;height:8px;border-radius:50%;background:var(--yellow)"></span><span id="themeLabel"></span></div>
    </header>
    <section style="background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);overflow:hidden">
      <div id="chips" style="display:grid;grid-template-columns:repeat(5,minmax(0,1fr))"></div>
      <div id="bar" style="display:flex;height:6px;background:var(--line)"></div>
    </section>
    <div style="position:sticky;top:56px;z-index:5;background:var(--bg);padding:18px 0 14px;margin-top:6px;display:flex;align-items:center;gap:12px;flex-wrap:wrap;border-bottom:1px solid var(--line)">
      <div id="statusBtns" style="display:flex;gap:2px;padding:3px;border:1px solid var(--line);border-radius:10px;background:var(--surface)"></div>
      <button onclick="toggleMine()" style="display:flex;align-items:center;gap:9px;border:1px solid var(--line);border-radius:10px;padding:8px 13px;font:inherit;font-size:13px;font-weight:500;cursor:pointer;background:var(--surface);color:var(--text)"><span id="mineTrack" style="width:26px;height:15px;border-radius:20px;background:var(--line2);position:relative;transition:background .2s"><span id="mineKnob" style="position:absolute;top:2px;left:2px;width:11px;height:11px;border-radius:50%;background:var(--surface);transition:left .2s"></span></span>Mine only</button>
      <div style="flex:1;min-width:220px;display:flex;align-items:center;gap:10px;border:1px solid var(--line);border-radius:10px;padding:0 13px;background:var(--surface)"><span class="mono" style="font-size:13px;color:var(--faint)">/</span><input id="q" oninput="onSearch(this.value)" placeholder="filter by name…" style="flex:1;border:0;outline:0;background:transparent;color:var(--text);font-family:'IBM Plex Mono',monospace;font-size:13px;padding:9px 0"></div>
      <div style="font-size:12.5px;color:var(--muted);white-space:nowrap"><span class="mono" id="shown" style="color:var(--text)"></span> shown</div>
    </div>
    <div id="sections"></div>
    <footer class="mono" style="margin-top:56px;padding-top:18px;border-top:1px solid var(--line);font-size:11.5px;color:var(--muted)">Generated by inventory.py · read-only · cleanup is a human decision</footer>
  </main>
</div>
</div>
<script>
const INV=/*__DATA__*/;
const SHORT={heartbeats:"Heartbeats",containers:"Containers",services:"Services",timers:"Timers",crons:"Crons",nginx:"Vhosts",ports:"Ports",certs:"Certs",dns:"DNS",sockets:"Sockets",pm2:"PM2",packages:"Packages",users:"Users",volumes:"Volumes",networks:"Networks",images:"Images",system:"System"};
const COL={live:"var(--live)",dormant:"var(--dormant)",broken:"var(--broken)",info:"var(--info)"};
const RING={live:"var(--live-t)",dormant:"var(--dormant-t)",broken:"var(--broken-t)",info:"var(--info-t)"};
const SUN=[[6.67,17.6],[6.5,17.9],[6.1,18.15],[5.6,18.35],[5.25,18.6],[5.1,18.85],[5.25,18.8],[5.5,18.5],[5.75,18.0],[5.9,17.6],[6.15,17.25],[6.5,17.25]];
let S={filter:"all",q:"",mine:false,cat:"all"};
function localNow(){const d=new Date();return {m:d.getMonth(),h:d.getHours()+d.getMinutes()/60,hh:String(d.getHours()).padStart(2,"0"),mm:String(d.getMinutes()).padStart(2,"0")};}
function isDark(){const n=localNow();const[rise,set]=SUN[n.m];return!(n.h>=rise&&n.h<set);}
function applyTheme(){document.documentElement.dataset.theme=isDark()?"dark":"light";const n=localNow();document.getElementById("themeLabel").textContent=(isDark()?"Night":"Day")+" theme · local "+n.hh+":"+n.mm;}
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[m]));}
function pass(it){const okS=S.filter==="all"?true:S.filter==="abandoned"?it.abandoned:it.status===S.filter;const okQ=!S.q||(it.name+" "+it.detail).toLowerCase().includes(S.q.toLowerCase());const okM=!S.mine||(it.meta&&it.meta.scope)==="local";return okS&&okQ&&okM;}
function parts(it){return String(it.detail||"").replace(/\s*\n\s*/g," ").split(" · ").filter(Boolean);}
function build(){
  document.getElementById("gen").textContent="generated "+INV.generated_at+" · "+INV.host;
  let nav='<div class="mono" style="font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--faint);padding:0 12px 8px">On this page</div><button class="navbtn" id="navAll" onclick="setCat(\'all\')"><span>All components</span><span class="mono" id="allN" style="font-size:11.5px;opacity:.75"></span></button><div style="height:1px;background:var(--line);margin:6px 12px"></div>';
  INV.categories.forEach(c=>{const br=c.items.filter(i=>i.status==="broken").length;
    nav+=`<button class="navbtn" data-nav="${c.key}" onclick="setCat('${c.key}')"><span style="display:flex;align-items:center;gap:8px"><span>${SHORT[c.key]||c.label}</span>${br?`<span class="mono" style="font-size:10.5px;font-weight:500;color:var(--broken-txt);background:var(--broken-t);border-radius:20px;padding:0 6px">${br}</span>`:""}</span><span class="mono" data-n="${c.key}" style="font-size:11.5px;opacity:.65"></span></button>`;});
  document.getElementById("nav").innerHTML=nav;
  const cn=INV.counts,sum=(cn.live+cn.dormant+cn.broken+cn.info)||1;
  document.getElementById("chips").innerHTML=[["Live","live",cn.live],["Dormant","dormant",cn.dormant],["Broken","broken",cn.broken],["Info","info",cn.info],["Abandoned","amber",INV.abandoned]].map(([label,k,n],i)=>`<div style="display:flex;flex-direction:column;gap:4px;padding:18px 22px;border-left:${i?"1px solid var(--line)":"0"}"><div style="display:flex;align-items:center;gap:8px;font-size:12px;font-weight:600;color:var(--muted);letter-spacing:.02em"><span style="width:8px;height:8px;border-radius:${k==="amber"?"2px":"50%"};background:${k==="amber"?"var(--yellow)":COL[k]}"></span>${label}</div><div class="mono" style="font-size:30px;font-weight:500;letter-spacing:-.02em;line-height:1.15;color:${((k==="broken"||k==="amber")&&n>0)?(k==="amber"?"var(--amber-txt)":"var(--broken-txt)"):(n===0?"var(--faint)":"var(--text)")}">${n}</div></div>`).join("");
  document.getElementById("bar").innerHTML=["live","info","dormant","broken"].map(k=>`<div style="height:100%;width:${cn[k]/sum*100}%;background:${COL[k]}"></div>`).join("");
  document.getElementById("statusBtns").innerHTML=[["All","all"],["Live","live"],["Dormant","dormant"],["Broken","broken"],["Abandoned","abandoned"]].map(([l,k])=>`<button class="segbtn" data-f="${k}" onclick="setFilter('${k}')">${k==="all"?"":`<span style="width:7px;height:7px;border-radius:${k==="abandoned"?"2px":"50%"};background:${k==="abandoned"?"var(--yellow)":COL[k]}"></span>`}${l}</button>`).join("");
  let sec="";
  INV.categories.forEach(c=>{
    const rows=c.items.map((it,i)=>{const st=it.status;let nm=it.name||"",tag="";const m=nm.match(/^(.*?)\s+\[(.+)\]$/);if(m){nm=m[1];tag=m[2];}
      const ps=parts(it).map((t,j)=>`<span style="display:flex;gap:8px;min-width:0;overflow-wrap:anywhere">${j?`<span style="color:var(--faint)">·</span>`:""}<span style="color:${j===0&&st==="broken"?"var(--broken-txt)":"var(--muted)"}">${esc(t)}</span></span>`).join("");
      const dotBg=it.abandoned?"var(--yellow)":(COL[st]||COL.info),ringBg=it.abandoned?"var(--yellow-t)":(RING[st]||RING.info);
      const nmColor=st==="dormant"?"var(--text2)":st==="broken"?"var(--broken-txt)":"var(--text)";
      const rColor=st==="broken"?"var(--broken-txt)":"var(--amber-txt)";
      return `<div class="row hov" style="display:grid;grid-template-columns:36px minmax(220px,34%) minmax(0,1fr);align-items:start;border-top:${i?"1px solid var(--line)":"0"};background:${st==="broken"?"var(--broken-t)":"transparent"}"><div style="padding:13px 0 0 18px"><span style="display:block;width:8px;height:8px;border-radius:50%;background:${dotBg};box-shadow:0 0 0 3px ${ringBg}"></span></div><div style="padding:9px 16px 9px 4px;min-width:0;display:flex;flex-direction:column;gap:3px"><div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap"><span class="mono" style="font-size:13px;color:${nmColor};overflow-wrap:anywhere">${esc(nm)}</span>${tag?`<span class="mono" style="font-size:10.5px;color:var(--muted);border:1px solid var(--line);border-radius:5px;padding:0 5px">${esc(tag)}</span>`:""}${it.abandoned?`<span style="font-size:10.5px;font-weight:600;letter-spacing:.03em;color:var(--amber-txt);background:var(--amber-t);border-radius:20px;padding:1px 8px">abandoned</span>`:""}</div>${it.reason?`<div style="font-size:12px;color:${rColor}">↳ ${esc(it.reason)}</div>`:""}</div><div style="padding:9px 20px 9px 0;display:flex;flex-wrap:wrap;column-gap:8px;row-gap:2px;font-size:13px;min-width:0">${ps}</div></div>`;}).join("");
    const bd=[["live"],["dormant"],["broken"],["info"]].map(([k])=>({k,n:c.items.filter(i=>i.status===k).length})).filter(x=>x.n).map(x=>`<span style="display:flex;align-items:center;gap:6px"><span style="width:6px;height:6px;border-radius:50%;background:${COL[x.k]}"></span><span class="mono" style="color:var(--text2)">${x.n}</span>${x.k}</span>`).join("").concat(c.items.some(i=>i.abandoned)?`<span style="display:flex;align-items:center;gap:6px"><span style="width:6px;height:6px;border-radius:2px;background:var(--yellow)"></span><span class="mono" style="color:var(--text2)">${c.items.filter(i=>i.abandoned).length}</span>abandoned</span>`:"");
    sec+=`<section class="cat" data-cat="${c.key}" style="margin-top:30px;display:flex;flex-direction:column;gap:10px"><div style="display:flex;align-items:baseline;justify-content:space-between;gap:16px;padding:0 2px"><div style="display:flex;align-items:baseline;gap:10px"><h2 style="margin:0;font-size:16px;font-weight:600;letter-spacing:-.005em">${esc(c.label)}</h2><span class="mono" data-cl="${c.key}" style="font-size:12px;color:var(--muted)"></span></div><div style="display:flex;gap:14px;font-size:12px;color:var(--muted)">${bd}</div></div><div class="rows" style="background:var(--surface);border:1px solid var(--line);border-radius:12px;box-shadow:var(--shadow);overflow:hidden">${rows}</div><div class="empty mono hidden" style="border:1px dashed var(--line2);border-radius:12px;padding:16px 18px;font-size:12.5px;color:var(--muted)"></div></section>`;});
  document.getElementById("sections").innerHTML=sec;
}
function setCat(k){S.cat=(S.cat===k?"all":k);apply();}
function setFilter(k){S.filter=k;apply();}
function toggleMine(){S.mine=!S.mine;apply();}
function onSearch(v){S.q=v;apply();}
function apply(){
  document.querySelectorAll("[data-f]").forEach(b=>b.classList.toggle("on",b.dataset.f===S.filter));
  document.querySelectorAll("[data-nav]").forEach(b=>b.classList.toggle("on",b.dataset.nav===S.cat));
  document.getElementById("navAll").classList.toggle("on",S.cat==="all");
  document.getElementById("mineTrack").style.background=S.mine?"var(--live)":"var(--line2)";
  document.getElementById("mineKnob").style.left=S.mine?"13px":"2px";
  document.getElementById("allN").textContent=INV.categories.reduce((a,c)=>a+c.items.filter(pass).length,0);
  INV.categories.forEach(c=>{const el=document.querySelector('[data-n="'+c.key+'"]');if(el)el.textContent=c.items.filter(pass).length;});
  let shown=0;
  document.querySelectorAll(".cat").forEach(sec=>{const key=sec.dataset.cat;const items=INV.categories.find(c=>c.key===key).items;const catOk=(S.cat==="all"||S.cat===key);let vis=0;
    sec.querySelectorAll(".row").forEach((r,i)=>{const v=catOk&&pass(items[i]);r.classList.toggle("hidden",!v);if(v)vis++;});
    sec.classList.toggle("hidden",!catOk);
    const cl=sec.querySelector("[data-cl]");if(cl)cl.textContent=(vis===items.length?items.length:vis+" of "+items.length);
    const rb=sec.querySelector(".rows"),em=sec.querySelector(".empty");
    if(catOk&&vis===0){rb.classList.add("hidden");em.classList.remove("hidden");em.textContent=items.length===0?"none found on this box":"all filtered out by the current filters — clear a filter to see them";}
    else{rb.classList.remove("hidden");em.classList.add("hidden");}
    shown+=vis;});
  document.getElementById("shown").textContent=shown;
}
function init(){applyTheme();setInterval(applyTheme,60000);build();apply();}
window.addEventListener("DOMContentLoaded",init);
</script><script>__BADGEJS__</script></body></html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(HERE, "index.html"))
    ap.add_argument("--json", action="store_true", help="also dump raw JSON")
    args = ap.parse_args()
    inv = build_inventory()
    with open(args.out, "w") as fh:
        fh.write(render_html(inv))
    if args.json:
        with open(os.path.splitext(args.out)[0] + ".json", "w") as fh:
            json.dump(inv, fh, indent=1, ensure_ascii=False)
    c = inv["counts"]
    print(f"wrote {args.out} — {c['live']} live, {c['dormant']} dormant, "
          f"{c['broken']} broken, {inv['abandoned']} abandoned")


if __name__ == "__main__":
    main()
