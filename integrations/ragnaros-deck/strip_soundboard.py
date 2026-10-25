"""Soundboard page strip, drawn with deckui (used by ragnarosd-plus `strip: live: soundboard`)."""
import json
import time

import deckui as u

AI_LOOK = {"off": ("OFF", "slate"), "connecting": ("LINKING", "amber"), "ready": ("READY", "cyan"),
           "listening": ("LISTENING", "red"), "thinking": ("THINKING", "amber"),
           "speaking": ("SPEAKING", "green"), "error": ("ERROR", "red"), "offline": ("NO LINK", "red")}


def state(ai, vol):
    """Everything the strip shows; also its per-panel signatures (cheap, no drawing)."""
    st = ai.get("state", "offline") if ai and time.time() - ai.get("ts", 0) < 120 else "offline"
    model = (ai.get("profile") or "").replace("Local · ", "").replace("Gemini ", "Gemini ")
    if st == "listening" and ai.get("listening_source") == "squad":
        model = "squad asked"
    reply = (ai.get("reply") or "").strip() or (ai.get("question") and f"“{ai['question']}”") or "Say “Hey Hasaan”…"
    v = {"vol": vol, "st": st, "model": model, "session": bool(ai.get("session_on")), "error": ai.get("error"),
         "mic": ai.get("mic_muted"), "hv": ai.get("headset_volume"), "hm": ai.get("headset_muted"), "reply": reply}
    sigs = [json.dumps(v["vol"]), json.dumps([st, model, v["session"], v["error"]]),
            json.dumps([v["mic"], v["hv"], v["hm"]]), json.dumps(reply)]
    return v, sigs


class _One(u.Strip):
    """A single 176-wide panel; panels are drawn (and cached) one at a time."""
    W = 176


def draw_panel(i, v):
    s = _One()
    if i == 0:                                   # soundboard volume (knob 0)
        vol = v["vol"]
        s.panel(0, "SOUNDBOARD", "orange", "turn: volume  ·  tap: stop", glyph="speaker")
        s.big(0, "--" if vol is None else f"{vol}%", "orange" if vol else u.DIM, y=26, size=44)
        s.bar(0, (vol or 0) / 100, "orange", y=80)
    elif i == 1:                                 # Hasaan
        label, col = AI_LOOK.get(v["st"], (v["st"].upper(), "slate"))
        s.panel(0, "HASAAN", "cyan", "tap: next model", active=v["st"] != "offline", glyph="bot")
        s.big(0, label, col, y=28, size=30)
        s.small(0, v["model"] or "-", 64, u.TEXT, 13, 600)
        s.small(0, (v["error"] or ("session on" if v["session"] else "session off"))[:30], 81,
                u.ACCENT["red"] if v["error"] else u.DIM, 11, 500)
    elif i == 2:                                 # mic -> PS5 + headset volume (knob 2)
        mic = v["mic"]
        word, mc = ("MUTED", "red") if mic else ("LIVE", "green") if mic is False else ("?", "slate")
        s.panel(0, "MIC → PS5", "violet", "turn: headset  ·  press: mic", glyph="mic")
        s.big(0, word, mc, y=26, size=36)
        hv, hm = v["hv"], v["hm"]
        s.small(0, "HEADSET --" if hv is None else ("HEADSET OFF" if hm else f"HEADSET {hv}%"), 68, u.TEXT, 12, 700)
        s.bar(0, 0 if (hm or hv is None) else hv / 100, "violet", y=86, x=14, w=148, h=6)
    else:                                        # last reply
        s.panel(0, "LAST REPLY", "green", "tap: show all", glyph="bars")
        s.text_block(0, v["reply"], y=29, size=12, lines=5)
    return s.image()


class Renderer:
    """Redraws only the panels whose signature changed (a full 3x draw is ~190 ms)."""

    def __init__(self):
        self.cache = [(None, None)] * 4

    def __call__(self, ai, vol):
        from PIL import Image
        v, sigs = state(ai, vol)
        frame = Image.new("RGB", (704, 124), u.BG)
        for i, sig in enumerate(sigs):
            if self.cache[i][0] != sig:
                self.cache[i] = (sig, draw_panel(i, v))
            frame.paste(self.cache[i][1], (i * 176, 0))
        return frame, sigs


def draw(v):
    from PIL import Image
    frame = Image.new("RGB", (704, 124), u.BG)
    for i in range(4):
        frame.paste(draw_panel(i, v), (i * 176, 0))
    return frame


def tap_fx(img, idx, phase, t, hold=0.5):
    """Feedback on the tapped panel only (1x, cheap): pressed tint, working dots, done glow."""
    from PIL import ImageDraw
    x0, acc = idx * 176, u.ACCENT[PANEL_ACCENT[idx % 4]]
    box = (x0 + 4, 4, x0 + 171, 119)
    d = ImageDraw.Draw(img, "RGBA")
    if phase == "press":
        d.rounded_rectangle(box, radius=16, fill=acc + (55,), outline=acc, width=4)
    elif phase == "busy":
        d.rounded_rectangle(box, radius=16, outline=acc + (190,), width=3)
        for i in range(3):                       # three dots chasing each other
            lit = int(t * 6) % 3 == i
            cx = x0 + 134 + i * 11
            d.ellipse((cx - 4, 12, cx + 4, 20), fill=acc if lit else acc + (70,))
    elif phase == "done":
        a = int(220 * (1 - min(1.0, t / hold)))
        d.rounded_rectangle(box, radius=16, outline=acc + (max(60, a),), width=4)
    return img


PANEL_ACCENT = ["orange", "cyan", "violet", "green"]


_reply_cache = [None, None]


def render_reply(ai):
    """Full-width view of the last question + answer (tap on LAST REPLY), cached."""
    key = json.dumps([ai.get("question"), ai.get("reply")])
    if _reply_cache[0] != key:
        _reply_cache[:] = [key, _draw_reply(ai)]
    return _reply_cache[1]


def _draw_reply(ai):
    from PIL import ImageDraw

    class Wide(u.Strip):
        PW = 704                                 # one panel across the whole strip
    s = Wide()
    q = (ai.get("question") or "").strip()
    a = (ai.get("reply") or "").strip() or "Nothing yet. Say “Hey Hasaan”."
    d, _ = s.panel(0, "LAST REPLY", "green", glyph="bars")
    d.text(((s.W - 92) * u.SS, 10 * u.SS), "tap to close", font=u.font(10 * u.SS, 500), fill=u.DIM + (255,))
    y = 31
    if q:
        s.small(0, "YOU  " + q, y, u.DIM, 12, 600)
        y += 18
    s.text_block(0, a, y=y, size=14, lines=(112 - y) // 17, width=s.W - 28)
    return s.image(), json.dumps([q, a])
