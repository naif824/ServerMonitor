#!/usr/bin/env python3
"""The honest pager — signal-only.

Pages ONLY on symptoms the owner would feel; everything else waits for the
weekly digest. Every judgment is DERIVED from each component's own declaration
(restart policy / systemd enablement / heartbeat window) — there is no
maintained critical list to rot.

Delivery is optional and config-driven (config["telegram"]). With no bot token
configured the pager still evaluates and records its state; ``--dry`` prints
what it would send without sending or recording anything.

Symptoms that page (🔴):
  1. a declared-to-serve front door not serving (enabled vhost, upstream down)
  2. a declared-to-run service not running (container restart policy / enabled
     non-oneshot unit)  -> also covers headless bots
  3. a missed heartbeat (backup/job)  -> latent, caught before you need it
  4. a live cert expiring < cert_days, or disk >= disk_pct

Anti-noise: debounce (>=5 min), cooldown (~1 h), flapping = one message, one
batched message per cycle, self-expiring mutes surfaced in the digest.

Usage:
  alerts.py            # evaluate + page on change (cron, every 2 min)
  alerts.py --digest   # weekly all-clear (its absence is the alarm)
  alerts.py --dry      # print, never send and never record
"""
import argparse, fnmatch, json, os, re, subprocess, sys, time, urllib.parse, urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import config

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(config.BASE, "index.json")
STATE = os.path.join(config.STATE, "alerts.json")
MUTES = os.path.join(config.STATE, "mutes.json")
SUPPRESS = os.path.join(HERE, "suppress.json")   # intentional stops — never page
HB = os.path.join(HERE, "heartbeats.json")
HB_DIR = os.path.join(config.STATE, "hb")
INCIDENTS = os.path.join(config.STATE, "incidents")   # pager drops symptom files here
ENABLED = "/etc/nginx/sites-enabled"

# thresholds (config-driven, with safe defaults)
DISK_PCT = config.threshold("disk_pct", 90)
MEM_PCT = config.threshold("mem_pct", 90)
LOAD_PER_CORE = config.threshold("load_per_core", 2.0)
SWAP_GB = config.threshold("swap_gb", 32)
CERT_DAYS = config.threshold("cert_days", 7)

DEBOUNCE_S = 300          # broken this long before paging
COOLDOWN_S = 3600         # quiet per item after a page
FLAP_N = 3                # break/recover count within an hour that counts as flapping
FLAP_WINDOW = 3600
INDEX_MAX_AGE_S = 300     # self-check: monitor must regenerate within 2x the 2min cadence


def sh(cmd, t=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=t).stdout
    except Exception:
        return ""


def send(text, dry=False):
    """POST to Telegram when a bot token + chat id are configured, else skip."""
    token, chat = config.telegram()
    if dry or not token or not chat:
        if dry:
            print("--- TELEGRAM (dry) ---\n" + re.sub(r"<[^>]+>", "", text))
        return True
    data = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage",
                               data=data, timeout=15)
        return True
    except Exception as e:
        print("telegram send error:", e)
        return False


def now():
    return time.time()


def emit_signal(source, message, kind, key=None):
    """Record one signal as a JSON file in state/incidents/. Best-effort: NEVER
    raises, so paging and state bookkeeping can never be affected by a write
    problem. The monitor page reads these back for its "last alert" column."""
    try:
        os.makedirs(INCIDENTS, exist_ok=True)
        ts = time.time()
        safe = re.sub(r"[^A-Za-z0-9]+", "-", (key or message))[:60].strip("-") or "signal"
        rec = {"key": key, "source": source, "message": message, "kind": kind, "ts": ts}
        path = os.path.join(INCIDENTS, "%.3f-%s-%s.json" % (ts, kind, safe))
        with open(path, "w") as f:
            json.dump(rec, f)
        return rec
    except Exception as e:
        print("signal write error:", e)
        return None


def record_incidents(page):
    """Record one ACTION signal per paged symptom. Never raises."""
    try:
        for key, msg in page:
            emit_signal("pager", re.sub(r"<[^>]+>", "", msg), "action", key=key)
    except Exception:
        pass


def load(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


# ----------------------------------------------------------------- symptoms
def listening_ports():
    out = set()
    for flag in ("-ltnp", "-lunp"):
        for m in re.finditer(r":(\d+)\s", sh(["sudo", "-n", "ss", "-H", flag])):
            out.add(m.group(1))
    return out


def sym_front_doors(live):
    """Declared front doors whose front door does not actually serve.

    Probes the real HTTPS response (not the upstream port — a vhost may
    redirect at / and proxy only /api/, which is still 'serving'). Not serving
    = no response or 5xx.

    The domain list comes from config["front_doors"] when set; otherwise it is
    discovered from enabled nginx vhosts (server_name directives).
    """
    configured = config.front_doors()
    if configured:
        targets = [(d, None) for d in configured]
    else:
        if not os.path.isdir(ENABLED):
            return []
        targets, seen = [], set()
        for f in os.listdir(ENABLED):
            try:
                txt = open(os.path.join(ENABLED, f), errors="ignore").read()
            except Exception:
                continue
            names = [n for grp in re.findall(r"server_name\s+([^;]+);", txt) for n in grp.split()]
            names = [n for n in names if re.match(r"^([a-z0-9-]+\.)+[a-z]{2,}$", n)
                     and n not in ("example.com", "localhost")]
            if not names or names[0] in seen:
                continue
            seen.add(names[0])
            targets.append((names[0], None))

    def probe(t):
        dom, _ip = t
        code = sh(["curl", "-sk", "-o", "/dev/null", "-w", "%{http_code}",
                   "--max-time", "8", f"https://{dom}/"], t=12).strip()
        return dom, code

    with ThreadPoolExecutor(max_workers=10) as ex:
        res = list(ex.map(probe, targets))
    return [(f"front:{dom}",
             f"front door <b>{dom}</b> not serving (HTTP {code or 'no response'})")
            for dom, code in res if code in ("", "000") or code.startswith("5")]


def sym_declared_containers():
    """Containers whose restart policy says 'keep running' but are not running."""
    out = []
    ids = sh(["docker", "ps", "-aq"]).split()
    if not ids:
        return out
    fmt = "{{.Name}}|{{.State.Status}}|{{.HostConfig.RestartPolicy.Name}}"
    for line in sh(["docker", "inspect", "--format", fmt] + ids, t=60).splitlines():
        parts = line.split("|")
        if len(parts) != 3:
            continue
        name, state, restart = parts[0].lstrip("/"), parts[1], parts[2]
        if restart in ("always", "unless-stopped") and state != "running":
            out.append((f"container:{name}", f"container <b>{name}</b> is <b>{state}</b> (policy: {restart})"))
    return out


def sym_declared_services():
    """Enabled non-oneshot services that are failed/inactive."""
    out = []
    for line in sh(["systemctl", "list-units", "--state=failed", "--type=service",
                    "--no-legend", "--plain", "--no-pager"]).splitlines():
        p = line.split()
        if p and p[0].endswith(".service"):
            out.append((f"service:{p[0]}", f"service <b>{p[0]}</b> failed"))
    return out


def sym_heartbeats():
    out = []
    hb = config.heartbeats()
    for name, meta in hb.items():
        p = os.path.join(HB_DIR, name)
        if not os.path.exists(p):
            continue  # never ran yet -> not a symptom (avoid setup noise)
        try:
            last = float(open(p).read().strip())
        except Exception:
            last = os.path.getmtime(p)
        age_min = (now() - last) / 60
        if age_min > meta.get("max_age_min", 120):
            out.append((f"hb:{name}", f"heartbeat <b>{name}</b> stale {age_min/60:.1f}h (max {meta['max_age_min']/60:.1f}h)"))
    return out


def sym_certs():
    """Reuse the generator's cert data (index.json) — no second certbot call."""
    out = []
    for c in load(INDEX, {}).get("categories", []):
        if c.get("key") != "certs":
            continue
        for it in c["items"]:
            m = it.get("meta", {})
            days = m.get("days")
            if m.get("referenced") and days is not None and days < CERT_DAYS:
                out.append((f"cert:{it['name']}",
                            f"cert <b>{it['name']}</b> expires in {days}d"))
    return out


def sym_disk():
    out = []
    import shutil
    du = shutil.disk_usage("/")
    pct = du.used / du.total * 100
    if pct >= DISK_PCT:
        out.append(("disk:/", f"disk / at <b>{pct:.0f}%</b>"))
    return out


def sym_swap():
    """Swap pressure (folded in from perf_alarm.sh). >32G used (80% of 40G)."""
    out = []
    try:
        mi = {}
        with open("/proc/meminfo") as f:
            for line in f:
                if ":" in line:
                    k, v = line.split(":", 1)
                    mi[k] = int(v.split()[0])
        used_g = (mi["SwapTotal"] - mi["SwapFree"]) / (1024 * 1024)
        if used_g >= SWAP_GB:
            out.append(("swap:/", f"swap <b>{used_g:.0f}G</b> used (memory pressure)"))
    except Exception:
        pass
    return out


def sym_load():
    """Host oversubscribed (folded in from perf_alarm.sh). Uses the SAME 5-min
    window + threshold as the dashboard (load_per_core 2.0) so they agree."""
    out = []
    try:
        cores = os.cpu_count() or 1
        l5 = os.getloadavg()[1]
        lp = l5 / cores
        if lp >= LOAD_PER_CORE:
            out.append(("load:/", f"load <b>{l5:.1f}</b> on {cores} cores = {lp:.2f}/core"))
    except Exception:
        pass
    return out


def sym_selfcheck():
    out = []
    if os.path.exists(INDEX):
        age = now() - os.path.getmtime(INDEX)
        if age > INDEX_MAX_AGE_S:
            out.append(("monitor:stale", f"monitor stale: inventory last generated {age/60:.0f}m ago"))
    return out


def evaluate():
    live = listening_ports()
    syms = []
    for fn in (sym_front_doors,):
        syms += fn(live)
    syms += sym_declared_containers()
    syms += sym_declared_services()
    syms += sym_heartbeats()
    syms += sym_certs()
    syms += sym_disk()
    syms += sym_swap()
    syms += sym_load()
    syms += sym_selfcheck()
    return {k: msg for k, msg in syms}


# ----------------------------------------------------------------- anti-noise
def muted(key, mutes):
    for pat, until in mutes.items():
        if (pat == key or (pat.endswith("*") and key.startswith(pat[:-1]))) and until > now():
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--digest", action="store_true")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()
    cur = evaluate()
    # drop intentionally-stopped items entirely — no page, no digest, no state
    pats = load(SUPPRESS, {}).get("patterns", [])
    if pats:
        cur = {k: v for k, v in cur.items()
               if not any(fnmatch.fnmatch(k, p) for p in pats)}
    st = load(STATE, {})
    mutes = load(MUTES, {})
    ts = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M %Z")

    if args.digest:
        lines = [f"✅ {config.HOST} all-clear · {ts}",
                 f"critical symptoms now: {len(cur)}",
                 f"inventory last ran: {int((now()-os.path.getmtime(INDEX))/60) if os.path.exists(INDEX) else '?'} min ago"]
        act = {k: v for k, v in mutes.items() if v > now()}
        if act:
            lines.append("mutes: " + ", ".join(f"{k}→{datetime.fromtimestamp(v).strftime('%a')}" for k, v in act.items()))
        for k, msg in cur.items():
            lines.append("🔴 " + re.sub(r"<[^>]+>", "", msg))
        body = "\n".join(lines)
        if args.dry:
            print("--- SIGNAL (dry) pager/info ---\n" + body)
        else:
            emit_signal("pager", body, "info")
            send(body)
        return

    # state machine, three independent passes so nothing eats anything else.
    page = []
    # 1. record breaks and recoveries; prune old flap history
    for key in cur:
        rec = st.setdefault(key, {"first": 0, "last_page": 0, "flaps": [],
                                  "was": False, "flap_notified": False})
        rec["flaps"] = [t for t in rec["flaps"] if now() - t < FLAP_WINDOW]
        if not rec["flaps"]:               # settled a full window -> re-arm
            rec["flap_notified"] = False
        if not rec["was"]:                 # a NEW break (fresh OR re-break)
            rec["first"] = now()           # debounce starts fresh every break
            rec["flaps"].append(now())
        rec["was"] = True
    recover = []
    for key, rec in st.items():
        if key not in cur and rec["was"]:
            rec["was"] = False
            if rec["last_page"]:
                recover.append(key)

    # 2. flapping — independent of debounce/cooldown (a fast break/recover must
    #    still be seen as one flapping signal, not silently swallowed)
    flapping = set()
    for key, rec in st.items():
        rec["flaps"] = [t for t in rec["flaps"] if now() - t < FLAP_WINDOW]
        if len(rec["flaps"]) >= FLAP_N and not rec["flap_notified"]:
            rec["flap_notified"] = True
            flapping.add(key)
            page.append((key, "flapping (>=%d breaks/hr)" % FLAP_N))

    # 3. plain page for current symptoms
    for key, msg in cur.items():
        if key in flapping:
            continue
        rec = st[key]
        if muted(key, mutes) or now() - rec["first"] < DEBOUNCE_S \
                or (rec["last_page"] and now() - rec["last_page"] < COOLDOWN_S):
            continue                              # cooldown only if we ever paged
        rec["last_page"] = now()
        page.append((key, msg))

    # persist; prune keys that are neither broken nor recently active/flapping
    # (skipped entirely in --dry so a dry run never touches state)
    if not args.dry:
        st = {k: v for k, v in st.items()
              if k in cur or v["flaps"] or (now() - v["last_page"] < 86400)}
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        json.dump(st, open(STATE, "w"), indent=1)

    if page:
        items = [re.sub(r"<[^>]+>", "", m) for _, m in page]
        body = f"🔴 {config.HOST}: {len(page)} critical symptom(s) · {ts}\n\n" + \
               "\n".join("• " + m for m in items)
        if config.STATUS_URL:
            body += "\n\n" + config.STATUS_URL
        if args.dry:
            print("--- SIGNAL (dry) pager/info ---\n" + body)
            for _key, m in page:
                print("--- SIGNAL (dry) pager/action --- " + re.sub(r"<[^>]+>", "", m))
        else:
            # one batched human overview (info) + one action signal per symptom
            # (recorded for the monitor page's "last alert" column).
            emit_signal("pager", body, "info")
            record_incidents(page)
            send(body)
    if recover:
        rec = f"✅ recovered · {ts}\n" + "\n".join("• " + k for k in recover)
        if args.dry:
            print("--- SIGNAL (dry) pager/info ---\n" + rec)
        else:
            emit_signal("pager", rec, "info")
            send(rec)
    if not page and not recover:
        print(f"quiet ({len(cur)} symptoms, none newly pageable)")


if __name__ == "__main__":
    main()
