#!/usr/bin/env python3
"""Needs attention — every red item from the status pages, in one place.

Report-only. Reads index.json and monitor/index.json, applies the same red rules
each page uses, and writes attention/index.html (plus red-counts.json for the
top-bar badges).

Nothing here acts on anything: it is a presentation layer over existing data.
"""
import json
import os

import config
import redcounts
import shell

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(HERE, "attention")

SECTIONS = [
    ("inv", "Server inventory", "/", "a-inv", "No broken components."),
    ("mon", "Monitoring", "/monitor/", "a-mon", "Every monitor ran on schedule."),
]

SUN = [[6.67, 17.6], [6.5, 17.9], [6.1, 18.15], [5.6, 18.35], [5.25, 18.6], [5.1, 18.85],
       [5.25, 18.8], [5.5, 18.5], [5.75, 18.0], [5.9, 17.6], [6.15, 17.25], [6.5, 17.25]]


def esc(s):
    return (str(s if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;"))


def fonts_css():
    try:
        with open(os.path.join(HERE, "fonts.css")) as f:
            return f.read()
    except Exception:
        return ""


def rows_html(rows):
    out = []
    for i, r in enumerate(rows):
        out.append(
            '<div style="display:grid;grid-template-columns:36px minmax(0,1.1fr) minmax(0,1.5fr) minmax(0,1.1fr);'
            'gap:0 18px;align-items:start;padding:11px 20px 11px 0;border-top:'
            + ("1px solid var(--line)" if i else "0") + ';background:var(--broken-t)">'
            '<div style="padding:6px 0 0 18px"><span style="display:block;width:8px;height:8px;border-radius:50%;'
            'background:var(--broken);box-shadow:0 0 0 3px var(--surface)"></span></div>'
            '<div style="display:flex;flex-direction:column;gap:2px;min-width:0">'
            '<span class="mono" style="font-size:13px;color:var(--broken-txt);overflow-wrap:anywhere">'
            + esc(r["name"]) + "</span>"
            '<span style="font-size:11.5px;color:var(--muted)">' + esc(r["where"]) + "</span></div>"
            '<span style="font-size:13px;color:var(--text2);overflow-wrap:anywhere">' + esc(r["detail"]) + "</span>"
            '<span style="font-size:13px;color:var(--broken-txt);overflow-wrap:anywhere">' + esc(r["why"]) + "</span></div>")
    return "".join(out)


def render(counts, sources):
    loaded = sources["loaded"]
    rows_by = {"inv": sources["invRows"], "mon": sources["monRows"]}

    chips = []
    sections = []
    for i, (key, title, href, anchor, clear_text) in enumerate(SECTIONS):
        rows = rows_by[key]
        n = len(rows)
        is_loaded = loaded.get(key, False)
        shown = n if is_loaded else "\u2026"
        has_rows = n > 0
        clear = is_loaded and n == 0
        chips.append(
            '<a href="#' + anchor + '" style="display:flex;flex-direction:column;gap:4px;padding:18px 22px;'
            'border-left:' + ("1px solid var(--line)" if i else "0") + ';color:var(--text)">'
            '<div style="display:flex;align-items:center;gap:8px;font-size:12px;font-weight:600;color:var(--muted);'
            'letter-spacing:.02em"><span style="flex:none;width:8px;height:8px;border-radius:50%;background:'
            + ("var(--broken)" if n else "var(--live)") + '"></span>' + title + "</div>"
            '<div class="mono" style="font-size:30px;font-weight:500;letter-spacing:-.02em;line-height:1.15;color:'
            + ("var(--broken-txt)" if n else "var(--faint)") + '">' + str(shown) + "</div></a>")
        body = ""
        if has_rows:
            body = ('<div style="background:var(--surface);border:1px solid var(--line);border-radius:12px;'
                    'box-shadow:var(--shadow);overflow:hidden">' + rows_html(rows) + "</div>")
        elif clear:
            body = ('<div style="display:flex;align-items:center;gap:10px;border:1px dashed var(--line2);'
                    'border-radius:12px;padding:14px 18px;font-size:13px;color:var(--muted)">'
                    '<span style="flex:none;width:8px;height:8px;border-radius:50%;background:var(--live)"></span>'
                    + clear_text + "</div>")
        sections.append(
            '<section id="' + anchor + '" style="margin-top:32px;display:flex;flex-direction:column;gap:10px;'
            'scroll-margin-top:76px">'
            '<div style="display:flex;align-items:baseline;justify-content:space-between;gap:16px;padding:0 2px">'
            '<div style="display:flex;align-items:baseline;gap:10px">'
            '<h2 style="margin:0;font-size:16px;font-weight:600;letter-spacing:-.005em">' + title + "</h2>"
            '<span class="mono" style="font-size:12px;color:var(--muted)">' + str(shown) + "</span></div>"
            '<a href="' + href + '" style="font-size:12.5px;font-weight:500;color:var(--text2)">Open '
            + title + " \u2192</a></div>" + body + "</section>")

    total = counts["all"]
    total_label = "all clear" if total == 0 else ("%d red item%s" % (total, "" if total == 1 else "s"))
    if not all(loaded.values()):
        missing = [t for (k, t, _, _, _) in SECTIONS if not loaded.get(k)]
        total_label += " \u00b7 " + ", ".join(missing) + " unavailable"

    nav = "".join(
        '<a href="#' + anchor + '" style="display:flex;align-items:center;justify-content:space-between;gap:8px;'
        'border-radius:8px;padding:6px 12px;font-size:13.5px;font-weight:500;color:var(--text2)">'
        "<span>" + title + "</span>"
        '<span class="mono" style="font-size:10.5px;font-weight:500;border-radius:20px;padding:0 7px;color:'
        + ("var(--broken-txt)" if len(rows_by[key]) else "var(--live-txt)") + ";background:"
        + ("var(--broken-t)" if len(rows_by[key]) else "var(--live-t)") + '">'
        + str(len(rows_by[key]) if loaded.get(key) else "\u2026") + "</span></a>"
        for key, title, href, anchor, clear_text in SECTIONS)

    tpl = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex,nofollow">
<title>__HOST__ \u2014 needs attention</title>
<style>
__FONTS__
__TOKENS__
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--text);transition:background-color .4s ease}
html{scroll-behavior:smooth}
body{font-family:Figtree,system-ui,sans-serif;-webkit-font-smoothing:antialiased;font-size:14px;line-height:1.5}
a{color:var(--text);text-decoration:none}a:hover{color:var(--text2)}
::selection{background:var(--amber-t)}
.mono{font-family:'IBM Plex Mono',monospace}
@media(max-width:900px){.app{grid-template-columns:1fr !important}.aside{position:static !important;height:auto !important}}
</style></head><body>
<div style="display:flex;flex-direction:column;min-height:100vh;background:var(--bg);color:var(--text);font-family:Figtree,system-ui,sans-serif;font-size:14px;line-height:1.5">
__TOPBAR__
<div class="app" style="display:grid;grid-template-columns:272px minmax(0,1fr);flex:1">
  <aside class="aside" style="position:sticky;top:56px;height:calc(100vh - 56px);overflow-y:auto;border-right:1px solid var(--line);background:var(--surface2);display:flex;flex-direction:column;gap:22px;padding:26px 16px 22px">
    <div style="display:flex;align-items:center;gap:10px;padding:0 8px">
      <div class="mono" style="flex:none;width:30px;height:30px;border-radius:8px;background:var(--action);color:var(--on-action);display:grid;place-items:center;font-weight:500;font-size:13px">__HBADGE__</div>
      <div style="display:flex;flex-direction:column;line-height:1.3;min-width:0">
        <span style="font-weight:600;font-size:15px;letter-spacing:-.01em">Needs attention</span>
        <span class="mono" style="font-size:11.5px;color:var(--muted)">__HOST__ status</span>
      </div>
    </div>
    <nav style="display:flex;flex-direction:column;gap:1px">
      <div class="mono" style="font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--faint);padding:0 12px 8px">On this page</div>
      __NAV__
    </nav>
    <div style="margin-top:auto;display:flex;flex-direction:column;gap:9px;padding:14px 12px 0;border-top:1px solid var(--line);font-size:12px;color:var(--muted)">
      <div class="mono" style="font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;color:var(--faint)">Legend</div>
      <div style="display:flex;align-items:center;gap:8px"><span style="flex:none;width:8px;height:8px;border-radius:50%;background:var(--broken)"></span>red on its own page</div>
      <div style="display:flex;align-items:center;gap:8px"><span style="flex:none;width:8px;height:8px;border-radius:50%;background:var(--live)"></span>nothing red there</div>
      <div style="margin-top:6px;font-size:11.5px;line-height:1.45;color:var(--faint)">Read-only. Nothing on this page acts on its own.</div>
    </div>
  </aside>
  <main style="min-width:0;padding:0 40px 80px">
    <header style="display:flex;align-items:flex-end;justify-content:space-between;gap:24px;padding:34px 0 22px">
      <div style="display:flex;flex-direction:column;gap:6px">
        <h1 style="margin:0;font-family:Newsreader,Georgia,serif;font-weight:500;font-size:34px;letter-spacing:-.015em;line-height:1.1">Everything red, in one place</h1>
        <div class="mono" style="font-size:12px;color:var(--muted)">collected from the status pages \u00b7 __TOTAL__</div>
      </div>
      <div style="display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted);white-space:nowrap"><span style="flex:none;width:8px;height:8px;border-radius:50%;background:var(--yellow)"></span><span id="themeLabel"></span></div>
    </header>
    <section style="background:var(--surface);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);overflow:hidden">
      <div style="display:grid;grid-template-columns:repeat(2,minmax(0,1fr))">__CHIPS__</div>
    </section>
    __SECTIONS__
    <footer class="mono" style="margin-top:56px;padding-top:18px;border-top:1px solid var(--line);font-size:11.5px;color:var(--muted)">Collected from inventory.py and monitor.py \u00b7 read-only</footer>
  </main>
</div>
</div>
<script>
const SUN=__SUN__;
function localNow(){const d=new Date();return {m:d.getMonth(),h:d.getHours()+d.getMinutes()/60,hh:String(d.getHours()).padStart(2,"0"),mm:String(d.getMinutes()).padStart(2,"0")};}
function isDark(){const n=localNow();const[r,s]=SUN[n.m];return!(n.h>=r&&n.h<s);}
function applyTheme(){document.documentElement.dataset.theme=isDark()?"dark":"light";const n=localNow();var el=document.getElementById("themeLabel");if(el)el.textContent=(isDark()?"Night":"Day")+" theme \u00b7 local "+n.hh+":"+n.mm;}
applyTheme();setInterval(applyTheme,60000);
</script><script>__BADGEJS__</script></body></html>"""

    return (tpl.replace("__FONTS__", fonts_css())
               .replace("__TOKENS__", shell.TOKENS)
               .replace("__TOPBAR__", shell.topbar("all"))
               .replace("__HOST__", config.HOST)
               .replace("__HBADGE__", config.HOST_BADGE)
               .replace("__BADGEJS__", shell.BADGE_JS)
               .replace("__NAV__", nav)
               .replace("__TOTAL__", total_label)
               .replace("__CHIPS__", "".join(chips))
               .replace("__SECTIONS__", "".join(sections))
               .replace("__SUN__", json.dumps(SUN)))


def main():
    counts, sources = redcounts.collect(HERE)
    with open(os.path.join(HERE, "red-counts.json"), "w") as f:
        json.dump(counts, f)
        f.write("\n")
    os.makedirs(OUT_DIR, exist_ok=True)
    with open(os.path.join(OUT_DIR, "index.html"), "w") as f:
        f.write(render(counts, sources))
    print("wrote attention/index.html and red-counts.json \u2014 %s" % counts)


if __name__ == "__main__":
    main()
