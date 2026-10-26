#!/usr/bin/env python3
"""PS5 scene watcher: turns the live PS5 stream into facts Hasaan can read instantly.

Runs on the MacBook next to the PS5 Stream app (LaunchAgent me.crcmz.ps5scene), on its
own, nonstop, so the picture is always fresh no matter what Hasaan is doing:
  - every time the picture changes (and at least every 6 s) it hands the latest frame to
    Qwen3-VL on oMLX and gets structured facts back: game, screen (playing / dead /
    spectating / menu ...), who's being spectated, kills, score, what just happened;
  - it turns changes into a timeline of events ("Moiz died", "got a kill (4)",
    "spectating xXJackXx") and keeps the last 15 minutes;
  - GET /scene (bearer token) answers in a few ms with the current facts + timeline;
  - it keeps the last 30 frames (2 a second = 15 s, shrunk, memory only) so POST /recap can
    tell Hasaan what just happened when someone asks ("what happened?", "did you see that?").

Python 3.9 stdlib only. Reads the frame file the PS5 Stream app writes; never stores images.
MUSE AI (deck key): while on, frames + events also go to Muse via app.crcmz.me (muse_loop).
HUD (hud_loop): kill feed, mode, score and the scoreboard read at full 1080p, kept per game and
match in matches.json; GET /stats answers "how many kills / who killed me / what's the score".
"""
import base64
import collections
import json
import os
import re
import subprocess
import tempfile
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
RING_N, RING_EVERY_S, RING_PX = 30, 0.5, 384   # last 15 s of frames, shrunk for a fast multi-image look
RECAP_PICK = 8                                   # frames sent to the model, spread over those 15 s
RECAP_PROMPT = ("These are {n} frames from the last {secs} seconds of a PS5 player's screen, oldest first. "
                "The player is {me}. In 2-3 short factual sentences say what {me} did and how it went: "
                "kills, deaths, who killed him, clutches, fails, wins or losses, anything funny or "
                "embarrassing. If it's only menus, loading or a lobby, say that. Only what you can actually see.")
GAME_PROMPT = ("Which video game is this? Likely ones: Call of Duty: Modern Warfare 4, Call of Duty: Warzone, "
               "Call of Duty: Black Ops 6, Call of Duty: Black Ops 7, Battlefield 6, Battlefield 2042, Fortnite, "
               "Apex Legends, Rainbow Six Siege. Call of Duty has a square minimap top-left and the mode name "
               "under it. Answer with just the game's name, or - if you can't tell.")
GAME_FILE = SUPPORT / "game"     # set by POST /game ("mw4", "Battlefield 6", ...); missing/"auto" = guess
GAME_EVERY_S = 120
SCORE_RE = re.compile(r"^\s*(\d{1,3})\s*[-:\u2013]\s*(\d{1,3})\s*$")

lock = threading.Lock()
state = {"live": False, "static_s": 0, "now": None, "updated": 0.0, "frames": 0, "vision_ms": None}
events = []          # (time, text)
hold = {"until": 0.0}  # POST /hold: Hasaan is listening/thinking/speaking, leave the GPU to him
ring = collections.deque(maxlen=RING_N)    # (time, small jpeg), memory only
ANALYSIS_OFF = SUPPORT / "analysis_off"    # exists = SCREEN AI switched off on the deck (survives restarts)


def analysis_on():
    return not ANALYSIS_OFF.exists()


def set_analysis(on):
    if on:
        if ANALYSIS_OFF.exists():
            ANALYSIS_OFF.unlink()
    else:
        ANALYSIS_OFF.touch()
        ring.clear()
        with lock:
            state["now"] = None                 # don't keep serving what it saw before
    with lock:
        add_event("screen analysis on" if on else "screen analysis off")
    return analysis_on()
SHRINK_DIR = tempfile.mkdtemp(prefix="ps5scene-")   # sips needs files: scratch, emptied each time


def shrink(jpg, px=RING_PX):
    a, b = os.path.join(SHRINK_DIR, "in.jpg"), os.path.join(SHRINK_DIR, "out.jpg")
    try:
        with open(a, "wb") as f:
            f.write(jpg)
        subprocess.run(["sips", "-Z", str(px), a, "--out", b], capture_output=True, timeout=5)
        with open(b, "rb") as f:
            return f.read()
    finally:
        for x in (a, b):
            try:
                os.remove(x)
            except OSError:
                pass


def capture():
    """Its own thread, so frames keep coming while the vision model is busy."""
    last = None
    while True:
        time.sleep(RING_EVERY_S)
        if not state["live"] or not analysis_on():
            ring.clear()
            continue
        try:
            m = FRAME.stat().st_mtime_ns
            if m != last:
                last = m
                ring.append((time.time(), shrink(FRAME.read_bytes())))
        except Exception:
            pass


def recap(me):
    if not analysis_on():
        return {"ok": False, "why": "screen analysis is switched off"}
    frames = list(ring)
    if len(frames) < 2:
        return {"ok": False, "why": "no live picture"}
    step = max(1, len(frames) // RECAP_PICK)
    pick = frames[::-1][::step][:RECAP_PICK][::-1]          # newest always in, evenly back from it
    secs = max(1, int(frames[-1][0] - frames[0][0]))
    hold["until"] = max(hold["until"], time.time() + 15)    # the frame-by-frame describer waits
    t = time.time()
    content = [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(j).decode()}}
               for _, j in pick]
    content.append({"type": "text", "text": RECAP_PROMPT.format(n=len(pick), secs=secs, me=me)})
    try:
        r = http_json(OMLX, {"model": MODEL, "max_tokens": 90, "temperature": 0,
                             "messages": [{"role": "user", "content": content}]},
                      {"Authorization": "Bearer " + OMLX_KEY}, timeout=20)
        text = (r["choices"][0]["message"]["content"] or "").strip()
    finally:
        hold["until"] = time.time() + 1
    now = time.time()
    with lock:
        ev = [f"{int(now - et)}s ago: {e}" for et, e in events if e and now - et <= 60]
    return {"ok": True, "text": text, "frames": len(pick), "span_s": secs, "events": ev[-10:],
            "vision_ms": int((now - t) * 1000)}


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
    if text and muse["on"]:
        muse_q.append((now, text, dict(state["now"] or {})))
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
        if not live or not analysis_on():
            continue
        for mark in (60, 180, 600):                       # frozen picture: AFK / paused / idle spectate
            if static_s >= mark and mark not in marks:
                marks.add(mark)
                with lock:
                    add_event(f"picture frozen for {mark // 60} min" if mark >= 60 else "")
        if static_s < 5:
            marks.clear()
        if time.time() < hold["until"]:
            continue                                      # the facts we have stay served meanwhile
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
            note_screen(state["now"], d)
            keep = diff(state["now"], d)
            state.update(now=keep, updated=time.time(), frames=state["frames"] + 1,
                         vision_ms=int((time.time() - t) * 1000))
        if time.time() - last_game > GAME_EVERY_S or (fixed_game() and fixed_game() != (state["now"] or {}).get("game")):
            last_game = time.time()
            g = fixed_game()
            if not g:
                try:
                    sharp = FULL.read_bytes() if FULL.exists() and time.time() - FULL.stat().st_mtime < 5 else jpg
                    g = field(vlm(shrink(sharp, 1280), GAME_PROMPT, 16).splitlines()[0])
                except Exception:
                    g = None
            if g:
                with lock:
                    if g != (state["now"] or {}).get("game"):
                        add_event(f"playing {g}")
                    state["now"]["game"] = g


# MUSE AI (the deck key): while it's on and the stream is live, send Muse a frame every
# MUSE_FRAME_S plus every timeline event, through app.crcmz.me's MCP. The server refuses
# samples while the switch is off, and this checks first so nothing leaves the Mac then.
CRCMZ_DIR = Path.home() / ".crcmz"
MUSE_URL = "https://app.crcmz.me/mcp"
MUSE_FRAME_S, MUSE_STATUS_S, MUSE_PX = 5.0, 15.0, 768
muse = {"on": False, "session": None, "checked": 0.0, "last_frame": 0.0, "sent": 0, "error": None}
muse_q = collections.deque(maxlen=200)     # (time, event text, facts at that moment)


def muse_call(tool, args):
    token = (CRCMZ_DIR / "stream-token").read_text().strip()
    args = dict(args, person=(CRCMZ_DIR / "stream-person").read_text().strip())
    r = http_json(MUSE_URL, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                             "params": {"name": tool, "arguments": args}},
                  {"Authorization": "Bearer " + token}, timeout=15)
    res = r.get("result") or {}
    data = json.loads(res["content"][0]["text"])
    if res.get("isError") or (isinstance(data, dict) and data.get("error")):
        raise RuntimeError(str(data.get("error") if isinstance(data, dict) else data)[:120])
    return data


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t)) + time.strftime("%z", time.localtime(t))


def muse_loop():
    while True:
        time.sleep(1.0)
        now = time.time()
        try:
            if now - muse["checked"] >= MUSE_STATUS_S:
                muse["checked"] = now
                muse["on"] = bool(muse_call("stream_analysis_status", {}).get("enabled"))
            if not (muse["on"] and state["live"]):
                muse["session"] = None
                muse_q.clear()
                continue
            if not muse["session"]:
                muse["session"] = "ps5-" + time.strftime("%Y%m%d-%H%M%S")
            sid = muse["session"]
            while muse_q:
                t, text, facts = muse_q.popleft()
                muse_call("stream_sample_add", {"session_id": sid, "type": "event", "timestamp": iso(t),
                                                "event": {"text": text, **{k: facts.get(k) for k in (
                                                    "game", "screen", "spectating", "killed_by", "kills", "score")
                                                    if facts.get(k) is not None}}})
                muse["sent"] += 1
            if now - muse["last_frame"] >= MUSE_FRAME_S:
                muse["last_frame"] = now
                jpg = FRAME.read_bytes()
                facts = dict(state["now"] or {})
                caption = " · ".join(str(facts[k]) for k in ("game", "screen", "spectating") if facts.get(k))
                muse_call("stream_sample_add", {"session_id": sid, "type": "frame", "timestamp": iso(now),
                                                "image_base64": base64.b64encode(shrink(jpg, MUSE_PX)).decode(),
                                                "mime": "image/jpeg", **({"text": caption} if caption else {})})
                muse["sent"] += 1
            muse["error"] = None
        except Exception as e:  # unreachable or refused: re-check the switch first, in 5 s
            muse["error"] = f"{type(e).__name__}: {e}"[:160]
            muse["checked"] = 0.0
            time.sleep(5)


# ---- HUD + matches: what the scoreboard / kill feed say, per game, for Hasaan's "how am I doing" ----
# A second ffmpeg reads the stream at full 1080p (frame.jpg is 768 px: too small for kill-feed text),
# 2 frames a second, from the PS5 Stream app's own mediamtx. A HUD read crops the part of the
# screen where that game keeps its kill feed / score, at full resolution, so the text is legible.
FULL = SUPPORT / "frame_full.jpg"
MATCHES = SUPPORT / "matches.json"          # every match, per game (last 200), survives restarts
MY_NAMES_FILE = SUPPORT / "my_names"        # the player's in-game names, one per line
SQUAD_FILE = SUPPORT / "squad_names"        # the squad's names, one per line (they can be on the other team in private matches)
CLAN_TAGS = ("CRCMZ", "CRCM2", "CRCMS")     # the squad's clan tag, as written (and as the model misreads it)
HUD_EVERY_S = 2.5
# per game family: where the HUD text is (x, y, w, h as fractions) and what to ask about it
HUD_PROFILES = [
    {"id": "cod", "match": r"call of duty|modern warfare|warzone|black ops|\bmw\d?\b|\bbo\d\b|\bcod\b",
     "crop": (0.0, 0.0, 0.36, 1.0),
     "prompt": ("The left side of a Call of Duty screen at full resolution. Read only what is there:\n"
                "MODE: the game mode name, or -\n"
                "SCORE: the two team score numbers on the left, top number first, like 1-4, or -\n"
                "FEED: one line per kill-feed entry, newest last, as killer > victim (weapon only if it "
                "is written as text), or FEED: - if there is no kill feed. The kill feed is the small list "
                "of 'name icon name' lines; ignore KILLED BY cards, banners, perks and menus.\n"
                "Gamertags exactly as written.")},
    {"id": "generic", "match": r".", "crop": (0.0, 0.0, 1.0, 1.0),
     "prompt": ("A video game screen at full resolution. Read only what is there:\n"
                "MODE: the game mode, or -\n"
                "SCORE: the match score like 1-4, or -\n"
                "FEED: one line per kill-feed entry (usually a corner list of who killed whom), newest last, "
                "as killer > victim (weapon), or FEED: - if there is none\n"
                "Gamertags exactly as written.")},
]
CARD_PROMPT = ("A death / kill-cam screen. Read only what is there, one per line:\n"
               "BANNER: big text at the top such as FINAL KILL, or -\n"
               "KILLED BY: the killer's gamertag exactly as written, or -\n"
               "KILLED WITH: the weapon, or -\n"
               "BUILD: the attachments listed with the weapon, comma separated, or -")
BOARD_PROMPT = ("A game scoreboard or round-result screen. First line: BANNER: big result text such as "
                "ROUND WIN, ROUND LOSS, VICTORY or DEFEAT, plus the reason under it (e.g. JSOF ELIMINATED), or -. "
                "Then one line per player on a scoreboard, top to bottom, as: team | gamertag | kills | deaths | "
                "score | status (alive, dead, spectating, or killed by NAME if shown). Then a last line: "
                "TEAMS: our score - their score. - for anything not readable.")
FEED_RE = re.compile(r"^\s*(?:FEED:\s*)?[-•*]?\s*(?P<k>[^>\n]{2,32}?)\s*>\s*(?P<v>[^(\n]{2,32}?)\s*(?:\((?P<w>[^)]{1,40})\))?\s*$")
GAME_IDS = [(r"warzone", "warzone"), (r"black ops (?:7|vii)|\bbo7\b", "bo7"), (r"black ops (?:6|vi)|\bbo6\b", "bo6"),
            (r"modern warfare|\bmw4?\b", "mw4")]
full = {"proc": None, "path": None, "error": None}
matches = {"list": [], "cur": None}


GAME_NAMES = {"mw4": "Call of Duty: Modern Warfare 4", "warzone": "Call of Duty: Warzone",
              "bo6": "Call of Duty: Black Ops 6", "bo7": "Call of Duty: Black Ops 7"}


def fixed_game():
    try:
        g = GAME_FILE.read_text().strip()
    except OSError:
        return None
    return None if not g or g.lower() == "auto" else GAME_NAMES.get(g.lower(), g)[:60]


def set_game(g):
    g = (g or "auto").strip()[:60]
    GAME_FILE.write_text(g)
    with lock:
        if state["now"] is not None and fixed_game():
            state["now"]["game"] = fixed_game()
            add_event(f"playing {fixed_game()}")
        m = matches["cur"]
        if m and fixed_game():                                   # the guess was wrong: fix this match
            m["game"], m["game_name"] = game_id(fixed_game()), fixed_game()
            save_matches()
    return fixed_game() or "auto"


def clean_name(n):
    return re.sub(r"^\s*[\[(][^\])]{1,8}[\])]\s*", "", n or "").strip()       # "[CRCMZ] Name" -> "Name"


def my_names():
    try:
        names = [l.strip() for l in MY_NAMES_FILE.read_text().splitlines() if l.strip()]
    except OSError:
        names = []
    return names or ["InterestingSoup", "moiiz41510"]


def is_mate(raw):
    """A squad member (clan tag or squad list). Only a label: in private matches they play against him."""
    if not raw:
        return False
    if any(t in raw.upper() for t in CLAN_TAGS):
        return True
    try:
        names = [l.strip() for l in SQUAD_FILE.read_text().splitlines() if l.strip()]
    except OSError:
        names = []
    return any(same_name(clean_name(raw), n) for n in names)


def is_me(name):
    return bool(name) and any(same_name(clean_name(name), n) for n in my_names())


def game_id(name):
    low = (name or "").lower()
    for pat, gid in GAME_IDS:
        if re.search(pat, low):
            return gid
    return re.sub(r"[^a-z0-9]+", "-", low).strip("-") or "unknown"


def hud_profile(game):
    return next(p for p in HUD_PROFILES if re.search(p["match"], (game or "").lower()))


def crop(jpg, box):
    """Crop a fraction box out of a JPEG at full resolution (sips, like shrink)."""
    x, y, w, h = box
    if (x, y, w, h) == (0.0, 0.0, 1.0, 1.0):
        return jpg
    a, b = os.path.join(SHRINK_DIR, "cin.jpg"), os.path.join(SHRINK_DIR, "cout.jpg")
    try:
        with open(a, "wb") as f:
            f.write(jpg)
        W, H = 1920, 1080
        out = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", a], capture_output=True, text=True).stdout
        m = re.findall(r"pixel(?:Width|Height): (\d+)", out)
        if len(m) == 2:
            W, H = int(m[0]), int(m[1])
        subprocess.run(["sips", "-c", str(int(H * h)), str(int(W * w)), "--cropOffset", str(int(H * y)), str(int(W * x)),
                        a, "--out", b], capture_output=True, timeout=5)
        with open(b, "rb") as f:
            return f.read()
    finally:
        for p in (a, b):
            try:
                os.remove(p)
            except OSError:
                pass


def stream_path():
    try:
        items = http_json("http://127.0.0.1:9997/v3/paths/list", timeout=2).get("items") or []
        return next((i["name"] for i in items if i.get("ready")), None)
    except Exception:
        return None


def fullres():
    """Keep one ffmpeg writing frame_full.jpg (1080p, 2 fps) while live and analysis is on."""
    while True:
        time.sleep(2)
        p = full["proc"]
        want = state["live"] and analysis_on()
        path = stream_path() if want else None
        if p and (p.poll() is not None or not want or path != full["path"]):
            if p.poll() is None:
                p.terminate()
            full["proc"] = None
        if want and path and full["proc"] is None:
            full["path"] = path
            try:
                full["proc"] = subprocess.Popen(
                    ["/opt/homebrew/bin/ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                     "-rtsp_transport", "tcp", "-hwaccel", "videotoolbox", "-i", f"rtsp://127.0.0.1:8554/{path}",
                     "-an", "-vf", "fps=2", "-q:v", "3", "-f", "image2", "-update", "1", "-atomic_writing", "1",
                     str(FULL)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                full["error"] = None
            except OSError as e:
                full["error"] = str(e)[:120]


def load_matches():
    try:
        matches["list"] = json.loads(MATCHES.read_text())[-200:]
    except (OSError, ValueError):
        matches["list"] = []
    last = matches["list"][-1] if matches["list"] else None
    if last and not last.get("ended") and time.time() - last["started"] < 2 * 3600:
        matches["cur"] = last                        # a restart mid-match carries on with it


def save_matches():
    tmp = MATCHES.with_suffix(".tmp")
    tmp.write_text(json.dumps(matches["list"][-200:]))
    tmp.replace(MATCHES)


def new_match(game, mode=None):
    end_match()
    m = {"id": time.strftime("%Y%m%d-%H%M%S"), "game": game_id(game), "game_name": game or None, "mode": mode,
         "started": time.time(), "ended": None, "kills": 0, "deaths": 0, "victims": [], "killed_by": [],
         "score": None, "scores": [], "feed": [], "scoreboard": None}
    matches["cur"] = m
    matches["list"].append(m)
    return m


def end_match():
    m = matches["cur"]
    if m and not m["ended"]:
        m["ended"] = time.time()
        save_matches()
    matches["cur"] = None


def cur_match():
    """The match being played; a new one when the game changes or none is open."""
    game = (state["now"] or {}).get("game")
    m = matches["cur"]
    if m is None:
        return new_match(game)
    if game and m["game"] in ("unknown", ""):
        m["game"], m["game_name"] = game_id(game), game            # the name arrived after the start
    elif game and m["game"] != game_id(game):
        return new_match(game)                                     # switched games
    return m


def note_screen(prev, cur):
    """Match boundaries + deaths from the screen state the describer reads."""
    ps, cs = (prev or {}).get("screen"), cur.get("screen")
    # a match ends after a real stay in the lobby / menus (rounds pass through short loading
    # screens); the next play then opens a new one (cur_match)
    if cs in ("lobby", "menu"):
        matches.setdefault("lobby_since", time.time())
        if time.time() - matches["lobby_since"] > 25 and matches["cur"]:
            end_match()
    else:
        matches.pop("lobby_since", None)
    if cs in ("dead", "killcam") and ps not in ("dead", "killcam"):
        by = cur.get("killed_by")
        cur_match()["pending_death"] = {"t": time.time(), "by": None if is_me(by) else clean_name(by) or None,
                                        "mate": bool(by) and not is_me(by) and is_mate(by)}


def read_card(jpg):
    out = {}
    for line in vlm(shrink(jpg, 1600), CARD_PROMPT, 80).splitlines():
        k, _, v = line.partition(":")
        k = k.strip().upper()
        if k in ("BANNER", "KILLED BY", "KILLED WITH", "BUILD"):
            out[k] = field(v)
    return out


def settle_death(m, card=None):
    """Turn the pending death into a death, a final kill of yours, or nothing (round-end cam)."""
    p = m.pop("pending_death", None)
    if not p:
        return
    card = card or {}
    raw = card.get("KILLED BY") or ""
    killer = clean_name(raw) or p["by"]
    final = "FINAL" in (card.get("BANNER") or "").upper()
    mate = (is_mate(raw) or (not raw and p.get("mate"))) and not is_me(raw or killer)   # label only
    if killer and is_me(killer):
        m["kills"] += 1
        m["victims"].append("(final kill)")
        add_event(f"{my_names()[0]} got the final kill" + (f" with {card['KILLED WITH']}" if card.get("KILLED WITH") else ""))
    elif final:                            # the round-end cam: not his death
        add_event("final kill" + (f" by {killer}" if killer else "") + (f" ({card['KILLED WITH']})" if card.get("KILLED WITH") else ""))
    else:
        m["deaths"] += 1
        if killer:
            m["killed_by"].append(killer)
        m.setdefault("deaths_detail", []).append({"t": p["t"], "by": killer, "weapon": card.get("KILLED WITH"),
                                                  "build": card.get("BUILD"), "squad": mate})
    save_matches()


def note_feed(m, entries):
    """New kill-feed entries (each stays on screen for seconds: dedupe against the last 15 s)."""
    now = time.time()
    for k, v, w in entries:
        k, v = clean_name(k), clean_name(v)
        if not k or not v or k == v:
            continue
        if any(same_name(k, e["killer"]) and same_name(v, e["victim"]) and now - e["t"] < 15 for e in m["feed"][-12:]):
            continue
        m["feed"].append({"t": now, "killer": k, "victim": v, "weapon": w})
        m["feed"] = m["feed"][-150:]
        if is_me(k) and not is_me(v):
            m["victims"].append(v)
            m["kills"] = max(m["kills"], len(m["victims"]))
            add_event(f"{my_names()[0]} killed {v}" + (f" ({w})" if w else ""))
        elif is_me(v) and not is_me(k):
            if not m["killed_by"] or not same_name(m["killed_by"][-1], k):
                m["killed_by"].append(k)                   # (the screen read may have logged it already)
            add_event(f"{my_names()[0]} was killed by {k}" + (f" ({w})" if w else ""))


def read_hud(jpg, game):
    prof = hud_profile(game)
    out = vlm(crop(jpg, prof["crop"]), prof["prompt"], 120)
    mode = score = None
    feed = []
    for line in out.splitlines():
        line = line.strip()
        if line.upper().startswith("MODE:"):
            mode = field(line[5:])
        elif line.upper().startswith("SCORE:"):
            m = SCORE_RE.match(line[6:].strip() or "")
            score = f"{m.group(1)}-{m.group(2)}" if m else None
        else:
            m = FEED_RE.match(line)
            if m and field(m["k"]) and field(m["v"]):
                feed.append((m["k"].strip(), m["v"].strip(), field(m["w"] or "")))
    return mode, score, feed


def read_board(jpg):
    rows, teams, banner = [], None, None
    for line in vlm(shrink(jpg, 1600), BOARD_PROMPT, 450).splitlines():
        if line.upper().startswith("BANNER:"):
            banner = field(line[7:])
            continue
        if line.upper().startswith("TEAMS:"):
            m = SCORE_RE.match(line[6:].strip() or "")
            teams = f"{m.group(1)}-{m.group(2)}" if m else None
            continue
        p = [field(x) for x in line.split("|")]
        if len(p) >= 5 and p[1]:
            num = lambda s: int(s) if s and s.isdigit() else None
            rows.append({"team": p[0], "name": clean_name(p[1]), "kills": num(p[2]), "deaths": num(p[3]),
                         "score": num(p[4]), "status": p[5] if len(p) > 5 else None, "me": is_me(p[1]),
                         "squad": is_mate(p[1])})
    return {"t": time.time(), "rows": rows[:24], "teams": teams, "banner": banner} if (rows or banner) else None


def note_banner(m, banner):
    """ROUND WIN / ROUND LOSS / VICTORY / DEFEAT: tally per match (a banner stays up a few seconds)."""
    b = (banner or "").upper()
    res = "win" if re.search(r"\bWIN|VICTORY|WON\b", b) else "loss" if re.search(r"LOSS|DEFEAT|LOST", b) else None
    if not res:
        return
    last = (m.get("rounds") or [{}])[-1]
    if last.get("result") == res and time.time() - last.get("t", 0) < 20:
        return
    m.setdefault("rounds", []).append({"t": time.time(), "result": res, "text": banner})
    final = bool(re.search(r"VICTORY|DEFEAT", b))
    add_event(("match " if final else "round ") + ("won" if res == "win" else "lost") + f": {banner}")


def hud_loop():
    last = 0.0
    while True:
        time.sleep(0.5)
        scr = (state["now"] or {}).get("screen")
        if (not state["live"] or not analysis_on() or time.time() < hold["until"]
                or time.time() - last < HUD_EVERY_S or scr not in ("playing", "dead", "killcam", "spectating", "scoreboard")):
            continue
        last = time.time()
        try:
            if not FULL.exists() or time.time() - FULL.stat().st_mtime > 3:
                continue                                  # no fresh full-res frame (ffmpeg starting)
            jpg = FULL.read_bytes()
            game = (state["now"] or {}).get("game")
            pend = (matches["cur"] or {}).get("pending_death")
            if pend and scr in ("dead", "killcam"):
                card = read_card(jpg)
                with lock:
                    if card.get("KILLED BY") or card.get("BANNER") or time.time() - pend["t"] > 8:
                        settle_death(cur_match(), card)
                continue
            if pend and time.time() - pend["t"] > 8:
                with lock:
                    settle_death(cur_match())               # never got a card: count what the screen said
            if scr == "scoreboard":
                board = read_board(jpg)
                if board:
                    with lock:
                        m = cur_match()
                        note_banner(m, board.get("banner"))
                        if board["rows"]:
                            m["scoreboard"] = board
                        mine = next((r for r in board["rows"] if r["me"]), None)
                        if mine and mine.get("kills") is not None:
                            m["kills"] = max(m["kills"], mine["kills"])
                        if mine and mine.get("deaths") is not None:
                            m["deaths"] = max(m["deaths"], mine["deaths"])
                        save_matches()
                continue
            mode, score, feed = read_hud(jpg, game)
            with lock:
                m = cur_match()
                if mode and not m["mode"]:
                    m["mode"] = mode
                elif mode and m["mode"] and mode.lower() != m["mode"].lower():
                    m = new_match(game, mode)              # a different mode: a new match
                if score and score != m["score"]:
                    if m["score"] and sum(map(int, score.split("-"))) < sum(map(int, m["score"].split("-"))) and \
                            score in ("0-0", "0-1", "1-0"):
                        m = new_match(game, mode or m["mode"])   # scores reset: a new match
                    m["score"] = score
                    m["scores"].append((time.time(), score))
                note_feed(m, feed)
                save_matches()
        except Exception:
            continue


def stats():
    """Per game: the match being played, the last few, and today's totals (for Hasaan)."""
    with lock:
        m = matches["cur"]
        game = m["game"] if m else game_id((state["now"] or {}).get("game"))
        today = time.strftime("%Y%m%d")
        mine = [x for x in matches["list"] if x["id"].startswith(today)]
        totals = {}
        for x in mine:
            t = totals.setdefault(x["game"], {"matches": 0, "kills": 0, "deaths": 0})
            t["matches"] += 1
            t["kills"] += x["kills"]
            t["deaths"] += x["deaths"]

        def brief(x):
            return {k: x.get(k) for k in ("id", "game", "game_name", "mode", "kills", "deaths", "score",
                                          "victims", "killed_by", "deaths_detail", "started", "ended")} | {
                "rounds": {"won": sum(r["result"] == "win" for r in x.get("rounds", [])),
                           "lost": sum(r["result"] == "loss" for r in x.get("rounds", []))},
                "feed": [f"{e['killer']} > {e['victim']}" + (f" ({e['weapon']})" if e.get("weapon") else "")
                         for e in x["feed"][-10:]],
                "scoreboard": x.get("scoreboard")}
        return {"me": my_names(), "game": game, "live": state["live"], "analysis": analysis_on(),
                "match": brief(m) if m else None,
                "recent": [brief(x) for x in [y for y in matches["list"] if y["game"] == game and y is not m][-5:]],
                "today": totals, "fullres": {"running": bool(full["proc"] and full["proc"].poll() is None),
                                             "error": full["error"]}}


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
                "frames": state["frames"], "vision_ms": state["vision_ms"], "analysis": analysis_on(),
                "muse": {k: muse[k] for k in ("on", "session", "sent", "error")}}


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            return self.reply(200, {"ok": True, "live": state["live"]})
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            return self.reply(401, {"error": "token required"})
        if self.path.split("?")[0] == "/scene":
            return self.reply(200, scene())
        if self.path.split("?")[0] == "/analysis":
            return self.reply(200, {"enabled": analysis_on(), "live": state["live"]})
        if self.path.split("?")[0] == "/stats":
            return self.reply(200, stats())
        if self.path.split("?")[0] == "/game":
            return self.reply(200, {"game": fixed_game() or "auto", "seen": (state["now"] or {}).get("game")})
        self.reply(404, {"error": "not found"})

    def do_POST(self):
        if self.headers.get("Authorization") != "Bearer " + TOKEN:
            return self.reply(401, {"error": "token required"})
        path = self.path.split("?")[0]
        if path not in ("/hold", "/recap", "/analysis", "/game"):
            return self.reply(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if path == "/analysis":                  # {"set": "on" | "off" | "toggle"}: the deck's SCREEN AI key
                want = str(body.get("set", "toggle")).lower()
                if want not in ("on", "off", "toggle"):
                    return self.reply(400, {"error": 'body: {"set": "on" | "off" | "toggle"}'})
                on = (not analysis_on()) if want == "toggle" else want == "on"
                return self.reply(200, {"enabled": set_analysis(on)})
            if path == "/game":                      # {"game": "mw4" | "Battlefield 6" | "auto"}
                return self.reply(200, {"game": set_game(str(body.get("game") or "auto"))})
            if path == "/recap":                     # {"me": "gamertag"}: what just happened
                try:
                    return self.reply(200, recap(str(body.get("me") or "the player")[:32]))
                except Exception as e:
                    return self.reply(200, {"ok": False, "why": f"vision model: {type(e).__name__}"})
            s = float(body.get("s", 0))
        except (ValueError, AttributeError):
            return self.reply(400, {"error": 'body: {"s": seconds}'})
        hold["until"] = time.time() + max(0.0, min(s, 30.0))   # self-expiring: a lost "release" can't stall it
        self.reply(200, {"ok": True, "held_s": max(0.0, min(s, 30.0))})

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
    threading.Thread(target=capture, daemon=True).start()
    threading.Thread(target=muse_loop, daemon=True).start()
    load_matches()
    threading.Thread(target=fullres, daemon=True).start()
    threading.Thread(target=hud_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
