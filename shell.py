#!/usr/bin/env python3
"""Shared shell for the ServerMonitor pages — top bar, tokens, red-count badges.

Presentation only. This module emits markup and never touches data, discovery,
or paging logic. Every generator (inventory.py, monitor.py, attention.py) uses
the same shell so the pages look like one product.
"""
import config

# Design tokens — light + dark theme variables used by every page.
TOKENS = (
    ":root{--bg:#f4f1ec;--surface:#ffffff;--surface2:#faf8f5;--hover:#f8f6f2;--line:#e8e4dc;"
    "--line2:#d9d4ca;--text:#232019;--text2:#4a453c;--muted:#6f6a60;--faint:#a49d92;--live:#16a34a;"
    "--live-t:#dcfce7;--live-txt:#15803d;--dormant:#9ca3af;--dormant-t:#eef0f2;--broken:#dc2626;"
    "--broken-t:#fdecec;--broken-txt:#b91c1c;--yellow:#eab308;--yellow-t:#fdf3c4;--amber:#eab308;"
    "--amber-t:#fdf3c4;--amber-txt:#8a5a00;--info:#3b82f6;--info-t:#e3edfd;--action:#232019;"
    "--on-action:#ffffff;--shadow:0 1px 2px rgba(35,32,25,.05);color-scheme:light}\n"
    ':root[data-theme="dark"]{--bg:#15130f;--surface:#1d1a15;--surface2:#211e18;--hover:#25221c;'
    "--line:#2d2922;--line2:#3a352c;--text:#ece7dd;--text2:#cbc4b7;--muted:#9c9589;--faint:#6f695f;"
    "--live:#22c55e;--live-t:#163222;--live-txt:#4ade80;--dormant:#7c828c;--dormant-t:#262729;"
    "--broken:#ef4444;--broken-t:#3a1c1c;--broken-txt:#f87171;--yellow:#facc15;--yellow-t:#332b10;"
    "--amber:#facc15;--amber-t:#332b10;--amber-txt:#facc15;--info:#60a5fa;--info-t:#1a2433;"
    "--action:#ece7dd;--on-action:#15130f;--shadow:none;color-scheme:dark}"
)

# page key -> (label, href). Only the read-only pages shipped in this package.
TABS = [
    ("inv", "Server inventory", "/"),
    ("mon", "Monitoring", "/monitor/"),
    ("all", "Needs attention", "/attention/"),
]

_TAB_BASE = ("display:flex;align-items:center;justify-content:center;gap:8px;height:36px;"
             "padding:0 12px;border-radius:9px;font-size:13px")
_TAB_ON = _TAB_BASE + ";border:1px solid var(--action);background:var(--action);color:var(--on-action);font-weight:600"
_TAB_OFF = _TAB_BASE + ";border:1px solid var(--line);background:var(--surface);color:var(--text2);font-weight:500"

_BADGE = ('<span data-rc="{key}" style="display:none;flex:none;min-width:18px;height:18px;padding:0 5px;'
          "border-radius:9px;background:var(--broken);color:#fff;font-family:'IBM Plex Mono',monospace;"
          'font-size:10.5px;font-weight:500;line-height:18px;text-align:center"></span>')


def topbar(active):
    """Return the sticky top bar with red-count badge slots."""
    out = ['<nav style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:6px;'
           "padding:0 24px;height:56px;align-content:center;box-sizing:border-box;position:sticky;"
           'top:0;z-index:20;border-bottom:1px solid var(--line);background:var(--surface2)">']
    for key, label, href in TABS:
        style = _TAB_ON if key == active else _TAB_OFF
        out.append('<a href="%s" style="%s">%s%s</a>' % (href, style, label, _BADGE.format(key=key)))
    out.append("</nav>")
    return "".join(out)


# Fills the badge slots from /red-counts.json (same origin, no external request).
BADGE_JS = r"""
(function(){
  function fill(rc){
    document.querySelectorAll('[data-rc]').forEach(function(el){
      var n=rc[el.dataset.rc]||0;
      if(n>0){el.textContent=n;el.style.display='inline-block';} else {el.style.display='none';}
    });
  }
  try{
    fetch('/red-counts.json',{cache:'no-store'}).then(function(r){return r.ok?r.json():null;})
      .then(function(rc){if(rc)fill(rc);}).catch(function(){});
  }catch(e){}
})();
"""
