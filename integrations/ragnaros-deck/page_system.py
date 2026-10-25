"""The deck's System page: live opti3 stats and device health, drawn with deckui.

Data comes from bin/sysdeck (~/.local/state/ragnaros-plus/sys/state.json). sysdeck
renders the keys and the strip (so the deck daemon never spends time drawing them);
the daemon draws only the detail views, on demand.
"""
import json

import deckui as u

VIEWS = ["cpu", "ram", "temp", "disk", "net", "docker", "devices"]
DEVICES = [("pi", "PSAUDIO PI", "pi"), ("ha", "HOME ASSIST", "home"), ("mac", "MACBOOK", "laptop"),
           ("tunnels", "TUNNELS", "cloud"), ("internet", "INTERNET", "globe")]


def level(frac, warm=0.7, hot=0.9):
    return "red" if frac >= hot else "amber" if frac >= warm else None


def _rate(v):
    if v is None:
        return "--"
    return f"{v / 1e6:.1f}M" if v >= 1e6 else f"{v / 1e3:.0f}K"


def _gb(kb):
    return f"{kb / 1048576:.1f}"


# -- keys ------------------------------------------------------------------------
def keys(st):
    """{tag: (signature, draw)}; sysdeck redraws a key only when its signature changes."""
    out = {}
    c = st.get("cpu") or {}
    if c.get("pct") is not None:
        f = c["pct"] / 100
        out["sys.cpu"] = (["cpu", round(c["pct"]), c.get("load")],
                          lambda: u.gauge_icon("CPU", f"{c['pct']:.0f}%", f"load {c.get('load', '-')}", f,
                                               level(f) or "cyan", level(f)))
    r = st.get("ram") or {}
    if r.get("pct") is not None:
        f = r["pct"] / 100
        out["sys.ram"] = (["ram", round(r["pct"]), round(r.get("used", 0) / 104858)],
                          lambda: u.gauge_icon("RAM", f"{r['pct']:.0f}%", f"{_gb(r['used'])}/{_gb(r['total'])} GB",
                                               f, level(f, 0.8, 0.92) or "violet", level(f, 0.8, 0.92)))
    t = st.get("temp") or {}
    if t.get("c") is not None:
        f = max(0.0, min(1.0, (t["c"] - 30) / 65))
        hot = "red" if t["c"] >= 90 else "amber" if t["c"] >= 80 else None
        out["sys.temp"] = (["temp", round(t["c"])],
                           lambda: u.gauge_icon("CPU TEMP", f"{t['c']:.0f}°", t.get("fan") or "", f, hot or "green", hot))
    dk = st.get("disk") or {}
    if dk.get("pct") is not None:
        f = dk["pct"] / 100
        out["sys.disk"] = (["disk", round(dk["pct"]), dk.get("free")],
                           lambda: u.gauge_icon("DISK /", f"{dk['pct']:.0f}%", f"{dk['free']} free", f,
                                                level(f, 0.8, 0.92) or "blue", level(f, 0.8, 0.92)))
    dc = st.get("docker")
    if dc:
        bad = len(dc.get("bad") or [])
        sub = f"{bad} need a look" if bad else "all healthy"
        out["sys.docker"] = (["docker", dc["running"], dc["total"], bad],
                             lambda: u.status_icon("DOCKER", f"{dc['running']}/{dc['total']}", "box", True, sub,
                                                   accent="amber" if bad else "blue"))
    devs = st.get("devices") or {}
    for tag, caption, glyph in DEVICES:
        d = devs.get(tag)
        if d is None:
            continue
        word = d.get("word") or ("UP" if d.get("ok") else "DOWN")
        out[f"sys.{tag}"] = ([tag, d.get("ok"), word, d.get("sub")],
                             lambda d=d, caption=caption, glyph=glyph, word=word:
                             u.status_icon(caption, word, glyph, d.get("ok"), d.get("sub") or ""))
    return out


# -- strip -----------------------------------------------------------------------
class _One(u.Strip):
    W = 176


HINTS = ["turn: views  ·  press: close", "turn: Mac brightness", "turn: headset  ·  press: mic",
         "turn: deck brightness"]


def panels(st):
    """[(signature, draw)] for the four strip panels (graphs of the last ~2 minutes)."""
    c, r, n, t = (st.get(k) or {} for k in ("cpu", "ram", "net", "temp"))

    def cpu():
        s = _One()
        s.panel(0, "CPU", "cyan", HINTS[0], glyph="bars")
        s.big(0, f"{c.get('pct', 0):.0f}%", "cyan", y=24, size=28)
        s.spark(0, c.get("hist") or [], "cyan", y=58, h=34, vmax=100)
        return s.image()

    def ram():
        s = _One()
        s.panel(0, "RAM", "violet", HINTS[1], glyph="bars")
        s.big(0, f"{r.get('pct', 0):.0f}%", "violet", y=24, size=28)
        s.spark(0, r.get("hist") or [], "violet", y=58, h=34, vmax=100)
        return s.image()

    def net():
        s = _One()
        s.panel(0, "NETWORK", "blue", HINTS[2], glyph="globe")
        s.big(0, f"↓{_rate(n.get('down'))}  ↑{_rate(n.get('up'))}", "blue", y=26, size=22)
        s.spark(0, n.get("hist") or [], "blue", y=58, h=34)
        return s.image()

    def temp():
        s = _One()
        hot = "red" if (t.get("c") or 0) >= 90 else "amber" if (t.get("c") or 0) >= 80 else "green"
        s.panel(0, "CPU TEMP", hot, HINTS[3], glyph="dots")
        s.big(0, f"{t.get('c', 0):.0f}°", hot, y=24, size=28)
        s.spark(0, t.get("hist") or [], hot, y=58, h=34, vmax=100)
        return s.image()

    sig = lambda *v: json.dumps(v)
    return [(sig(c.get("hist")), cpu), (sig(r.get("hist")), ram), (sig(n.get("hist"), n.get("up")), net),
            (sig(t.get("hist"), t.get("fan")), temp)]


# -- detail views (the daemon draws these when a key / panel / knob 0 asks) --------
TITLES = {"cpu": ("TOP PROCESSES · CPU", "cyan", "bars"), "ram": ("TOP PROCESSES · MEMORY", "violet", "bars"),
          "temp": ("SENSORS", "green", "dots"), "disk": ("DISKS", "blue", "box"),
          "net": ("NETWORK", "blue", "globe"), "docker": ("DOCKER", "blue", "box"),
          "devices": ("DEVICES", "green", "pi")}


def detail_rows(view, st):
    """(header, [(name, value, frac or None, color or None)])"""
    if view == "cpu":
        c = st.get("cpu") or {}
        rows = [(n, f"{p:.0f}%", min(1.0, p / 100), None) for n, p in (st.get("procs_cpu") or [])]
        return f"{c.get('pct', 0):.0f}%  ·  load {c.get('load3', '-')}  ·  {c.get('cores', '?')} cores", rows
    if view == "ram":
        r = st.get("ram") or {}
        top = max([mb for _, mb in st.get("procs_ram") or []] or [1])
        rows = [(n, f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb:.0f} MB", mb / top, None)
                for n, mb in (st.get("procs_ram") or [])]
        return f"{_gb(r.get('used', 0))} / {_gb(r.get('total', 0))} GB  ·  swap {r.get('swap_pct', 0):.0f}%", rows
    if view == "temp":
        rows = []
        for n, val in (st.get("temp") or {}).get("sensors") or []:
            if isinstance(val, str):
                rows.append((n, val, None, None))
            else:
                rows.append((n, f"{val:.0f}°", max(0.0, min(1.0, (val - 30) / 65)),
                             "red" if val >= 90 else "amber" if val >= 80 else None))
        return "hottest reading per sensor", rows
    if view == "disk":
        rows = [(m, f"{p:.0f}%  {free}", p / 100, level(p / 100, 0.8, 0.92))
                for m, p, free in (st.get("disk") or {}).get("mounts") or []]
        return "used  ·  free", rows
    if view == "net":
        n = st.get("net") or {}
        rows = [(i, f"↓{_rate(d)} ↑{_rate(up)}", None, None) for i, d, up in n.get("ifaces") or []]
        return f"↓{_rate(n.get('down'))}/s  ↑{_rate(n.get('up'))}/s  ·  LAN {n.get('ip', '?')}", rows
    if view == "docker":
        dc = st.get("docker") or {}
        rows = [(name, status, None, "amber") for name, status in dc.get("bad") or []]
        if not rows:
            rows = [("everything", "running fine", None, "green")]
        return f"{dc.get('running', '?')} running of {dc.get('total', '?')}", rows
    if view == "devices":
        rows = []
        for tag, caption, _ in DEVICES:
            d = (st.get("devices") or {}).get(tag) or {}
            rows.append((caption.title(), f"{d.get('word', '?')}  {d.get('sub', '')}".strip(), None,
                         "green" if d.get("ok") else "red"))
        return "checked every 15 s", rows
    return "", []


def detail(view, st):
    title, accent, glyph = TITLES.get(view, (view.upper(), "cyan", "dots"))
    header, rows = detail_rows(view, st)

    class Wide(u.Strip):
        PW = 704
    s = Wide()
    from PIL import ImageDraw
    d, _ = s.panel(0, title, accent, glyph=glyph)
    SS = u.SS
    d = ImageDraw.Draw(s.img, "RGBA")
    hw = d.textlength(title, font=u.font(12 * SS, 700))
    d.text((32 * SS + hw + 14 * SS, 10 * SS), header, font=u.font(12 * SS, 500), fill=u.DIM + (255,))
    pos = VIEWS.index(view) if view in VIEWS else 0
    for j in range(len(VIEWS)):                                 # page dots: which view of 7
        cx = (704 - 20 - (len(VIEWS) - 1 - j) * 11) * SS
        col = u.ACCENT[accent] if j == pos else u.FAINT
        d.ellipse((cx - 3 * SS, 15 * SS, cx + 3 * SS, 21 * SS), fill=col + (255,))
    colw, top = 330, 33
    for k, (name, value, frac, color) in enumerate(rows[:8]):
        col, row = divmod(k, 4)
        x, y = 14 + col * (colw + 20), top + row * 21
        fv = u.font(12 * SS, 700)
        vw = d.textlength(value, font=fv) / SS
        fn = u.fit(d, name, 12 * SS, (colw - vw - 10) * SS, 500)
        d.text((x * SS, y * SS), name, font=fn, fill=u.TEXT + (255,))
        vc = u.ACCENT.get(color, u.TEXT) if color else u.TEXT
        d.text(((x + colw - vw) * SS, y * SS), value, font=fv, fill=vc + (255,))
        if frac is not None:
            s.bar(0, frac, color or accent, y=y + 16, x=x, w=colw, h=3)
    if not rows:
        d.text((14 * SS, top * SS), "no data yet", font=u.font(13 * SS, 500), fill=u.DIM + (255,))
    return s.image()
