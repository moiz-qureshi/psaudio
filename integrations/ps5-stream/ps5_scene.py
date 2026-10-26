#!/usr/bin/env python3
"""PS5 scene watcher: turns the live PS5 stream into facts Hasaan can read instantly.

Runs on the MacBook next to the PS5 Stream app (LaunchAgent me.crcmz.ps5scene), on its
own, nonstop, so the picture is always fresh no matter what Hasaan is doing:
  - every time the picture changes (and at least every 6 s) it hands the latest frame to
    Qwen3-VL on oMLX and gets structured facts back: game, screen (playing / dead /
    spectating / menu ...), who's being spectated, kills, score, what just happened;
  - it turns changes into a timeline of events ("Moiz died", "got a kill (4)",
    "spectating xXJackXx") and keeps the last 15 minutes;
  - GET /scene (bearer token) answers in a few ms with the current facts + timeline.

Python 3.9 stdlib only. Reads the frame file the PS5 Stream app writes; never stores images.
"""
import base64
import json
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SUPPORT = Path.home() / "Library/Application Support/PS5Stream"
FRAME = SUPPORT / "frame.jpg"
TOKEN = (SUPPORT / "token").read_text().strip()
OMLX_KEY = (SUPPORT / "omlx_key").read_text().strip()
OMLX = "http://127.0.0.1:1234/v1/chat/completions"
MODEL = "Qwen3-VL-8B-Instruct-MLX-4bit"
PORT = 8899
MIN_GAP_S, MAX_GAP_S = 1.2, 6.0      # describe at most this often / at least this often while live
KEEP_S = 15 * 60
SCREENS = ("playing", "dead", "killcam", "spectating", "menu", "lobby", "loading", "scoreboard", "map",
           "paused", "other")

# One short line per frame (much less to generate than JSON: ~15 tokens, so ~0.4 s of decoding).
PROMPT = """Moiz's PS5 screen, live. Answer with ONE line of 6 fields separated by |, and - for anything not visible:
screen (playing, dead, killcam, spectating, menu, lobby, loading, scoreboard, map, paused, other) | gamertag being spectated | gamertag who killed Moiz | Moiz's kill count | match score like 6-5 | what just happened, 2-5 words
Example: spectating | xXJackXx | - | 3 | 4-6 | teammate clutching
Only what you can actually read on screen."""
GAME_PROMPT = "Which video game is this? Answer with just its name (1-5 words), or - if you can't tell."
GAME_EVERY_S = 120
SCORE_RE = re.compile(r"^\s*(\d{1,3})\s*[-:\u2013]\s*(\d{1,3})\s*$")

lock = threading.Lock()
state = {"live": False, "static_s": 0, "now": None, "updated": 0.0, "frames": 0, "vision_ms": None}
events = []          # (time, text)


def http_json(url, body=None, headers=None, timeout=20):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def vlm(jpg, prompt, max_tokens):
    r = http_json(OMLX, {
        "model": MODEL, "max_tokens": max_tokens, "temperature": 0,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}},
            {"type": "text", "text": prompt}]}]}, {"Authorization": "Bearer " + OMLX_KEY}, timeout=30)
    return (r["choices"][0]["message"]["content"] or "").strip()


def field(v):
    v = (v or "").strip().strip('"').strip()
    return None if v.lower() in ("", "-", "none", "null", "n/a", "unknown") else v[:60]


def describe(jpg):
    line = next((l for l in vlm(jpg, PROMPT, 40).splitlines() if "|" in l), "")
    parts = [field(x) for x in line.split("|")] + [None] * 6
    if not line:
        return None
    scr = (parts[0] or "other").lower()
    d = {"screen": scr if scr in SCREENS else "other", "spectating": parts[1], "killed_by": parts[2],
         "kills": None, "score": None, "event": parts[5]}
    if parts[3] and parts[3].isdigit():
        d["kills"] = int(parts[3])
    m = SCORE_RE.match(parts[4] or "")
    if m:
        d["score"] = f"{m.group(1)}-{m.group(2)}"
    return d


def add_event(text):
    now = time.time()
    if events and events[-1][1] == text and now - events[-1][0] < 20:
        return
    events.append((now, text))
    while events and now - events[0][0] > KEEP_S:
        events.pop(0)


pending = {}          # field -> value seen once, waiting for a second read


def stable(field_name, value, confirmed):
    """True once `value` has been read twice in a row (VLM misreads are one-offs)."""
    if value is None or value == confirmed:
        pending.pop(field_name, None)
        return False
    if pending.get(field_name) == value:
        pending.pop(field_name, None)
        return True
    pending[field_name] = value
    return False


def same_name(a, b):
    """Gamertags as the VLM reads them drift a letter or two (JackRabbi1911ton / JackPabbi1911ion)."""
    import difflib
    if not a or not b:
        return False
    a, b = a.lower(), b.lower()
    return a == b or a.startswith(b) or b.startswith(a) or difflib.SequenceMatcher(None, a, b).ratio() > 0.75


def diff(prev, cur):
    """Changes between two reads -> timeline events. Returns the facts to keep."""
    p = prev or {}
    if same_name(cur.get("spectating"), p.get("spectating")):
        cur["spectating"] = p["spectating"]              # same teammate, misread: keep the first spelling
    keep = dict(cur, game=p.get("game"))
    ps, cs = p.get("screen"), cur["screen"]
    if cs != ps:
        if cs in ("dead", "killcam") and ps not in ("dead", "killcam"):
            add_event("Moiz died" + (f" (killed by {cur['killed_by']})" if cur.get("killed_by") else ""))
        elif cs == "spectating":
            add_event("Moiz is spectating " + (cur.get("spectating") or "a teammate"))
        elif cs == "playing" and ps in ("dead", "killcam", "spectating"):
            add_event("Moiz is back in the game")
        elif cs == "playing" and ps in ("loading", "lobby", "menu"):
            add_event("match started")
        elif cs in ("lobby", "menu") and ps in ("playing", "scoreboard", "dead", "spectating"):
            add_event("back in the " + cs)
        elif cs == "scoreboard":
            add_event("scoreboard up" + (f": {cur['score']}" if cur.get("score") else ""))
    elif cs == "spectating" and cur.get("spectating") and cur["spectating"] != p.get("spectating"):
        add_event("now spectating " + cur["spectating"])
    # kills / score: only once confirmed by a second read; otherwise keep the last good value
    if stable("kills", cur.get("kills"), p.get("kills")):
        if p.get("kills") is not None and cur["kills"] > p["kills"]:
            add_event(f"Moiz got a kill ({cur['kills']} now)")
    else:
        keep["kills"] = p.get("kills") if cur.get("kills") != p.get("kills") else cur.get("kills")
    if stable("score", cur.get("score"), p.get("score")):
        add_event(f"score {cur['score']}")
    else:
        keep["score"] = p.get("score")
    if cur.get("event") and cur["event"] != p.get("event") and stable("event", cur["event"], p.get("event")):
        add_event(cur["event"])
    return keep


def watch():
    last_desc, last_game, marks = 0.0, 0.0, set()
    while True:
        time.sleep(0.4)
        try:
            st = http_json("http://127.0.0.1:8898/status", headers={"Authorization": "Bearer " + TOKEN}, timeout=2)
        except Exception:
            st = {}
        live, static_s = bool(st.get("live")), int(st.get("static_s") or 0)
        with lock:
            if state["live"] and not live:
                add_event("stream stopped")
            if live and not state["live"]:
                add_event("stream started")
            state["live"], state["static_s"] = live, static_s
        if not live:
            continue
        for mark in (60, 180, 600):                       # frozen picture: AFK / paused / idle spectate
            if static_s >= mark and mark not in marks:
                marks.add(mark)
                with lock:
                    add_event(f"picture frozen for {mark // 60} min" if mark >= 60 else "")
        if static_s < 5:
            marks.clear()
        gap = time.time() - last_desc
        moving = float(st.get("motion") or 0) > 0.012
        if gap < MIN_GAP_S or (not moving and gap < MAX_GAP_S):
            continue
        try:
            jpg = FRAME.read_bytes()
            t = time.time()
            d = describe(jpg)
        except Exception:
            continue
        last_desc = time.time()
        if not d:
            continue
        with lock:
            keep = diff(state["now"], d)
            state.update(now=keep, updated=time.time(), frames=state["frames"] + 1,
                         vision_ms=int((time.time() - t) * 1000))
        if time.time() - last_game > GAME_EVERY_S:      # the game's name, now and then
            last_game = time.time()
            try:
                g = field(vlm(jpg, GAME_PROMPT, 12).splitlines()[0])
            except Exception:
                g = None
            if g:
                with lock:
                    if g != (state["now"] or {}).get("game"):
                        add_event(f"playing {g}")
                    state["now"]["game"] = g


def scene():
    with lock:
        now = time.time()
        cur = dict(state["now"] or {})
        age = round(now - state["updated"], 1) if state["updated"] else None
        bits = [x for x in (
            cur.get("game"), cur.get("screen"),
            f"spectating {cur['spectating']}" if cur.get("spectating") else None,
            f"killed by {cur['killed_by']}" if cur.get("killed_by") and cur.get("screen") in ("dead", "killcam") else None,
            f"{cur['kills']} kills" if cur.get("kills") is not None else None,
            f"score {cur['score']}" if cur.get("score") else None,
            f"frozen {state['static_s']}s" if state["static_s"] >= 20 else None) if x]
        return {"live": state["live"], "age_s": age, "static_s": state["static_s"], "now": cur or None,
                "summary": " · ".join(bits) + (f" — {cur['event']}" if cur.get("event") else ""),
                "events": [{"ago_s": int(now - t), "text": e} for t, e in events[-25:] if e],
                "frames": state["frames"], "vision_ms": state["vision_ms"]}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"ok": True, "live": state["live"]})
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            return self.reply(401, {"error": "token required"})
        if self.path.split("?")[0] == "/scene":
            return self.reply(200, scene())
        self.reply(404, {"error": "not found"})

    def reply(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    threading.Thread(target=watch, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
