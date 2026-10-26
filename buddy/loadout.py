"""Full classes from the wzmeta API, read out once, then (if wanted) one piece at a time.

  "what's the meta class for resurgence?"  -> long-range + close-range weapon with every
                                              attachment, then "Want me to go through it slow?"
  "yes"                                    -> "Primary, the VOYAK KT-3. Got it?"
  "next" / "got it" / "ok"                 -> the next piece        ("repeat", "back", "stop" work too)

Answers are built here, not by the model: names have to be exact and it is faster.
API: https://wzmeta.interestingsoup.com/api (games mw4, warzone, bo6, bo7).
"""
import re

import httpx

API = "https://wzmeta.interestingsoup.com/api"

CLASS_Q = re.compile(r"\b(class(?:es)?|load ?outs?|builds?|attachments?|meta|best (?:gun|weapon|smg|ar|"
                     r"assault rifle|sniper|shotgun|lmg|pistol|marksman|battle rifle)s?)\b", re.I)
GAMES = [  # (pattern, game id) - first match wins
    (r"\bwar ?zone|\bwz\b|resurgence|battle ?royale|\bbr\b|rebirth|iron gauntlet|black ops royale", "warzone"),
    (r"black ?ops ?(?:7|seven)|\bb\s?o\s?(?:7|seven)\b", "bo7"),
    (r"black ?ops ?(?:6|six)|\bb\s?o\s?(?:6|six)\b", "bo6"),
    (r"modern warfare|\bmw\s?(?:4|four)?\b|\bdmz\b", "mw4"),
]
MODES = ["ranked resurgence", "resurgence", "black ops royale", "iron gauntlet", "battle royale",
         "ranked", "dmz pvp", "dmz pve", "multiplayer"]
CATEGORY = [(r"\bsnipers?\b", "sniper"), (r"close[- ]range|\bsmgs?\b|shotguns?", "close-range"),
            (r"long[- ]range|\bars?\b|assault rifles?", "long-range")]
WEAPON_Q = re.compile(r"\b(?:class|build|load ?out|attachments?|setup)\s+(?:for|on)\s+(?:the|my|a)?\s*"
                      r"(?P<w>[\w.\- ]{2,30}?)\s*(?:in\b|for\b|on\b|[?.!,]|$)", re.I)

YES = re.compile(r"^\W*(yes|yeah|yep|yup|ya|sure|ok(?:ay)?|please|go|do it|go ahead|bet|alright|"
                 r"slow(?:er)?|one by one)\b", re.I)
NO = re.compile(r"^\W*(no|nah|nope|i'?m good|all good|never ?mind|stop|cancel)\b", re.I)
NEXT = re.compile(r"\b(next|got it|gotcha|ok(?:ay)?|yes|yeah|yep|yup|done|go|continue|ready|cool|alright|"
                  r"sure|k|keep going|go on|and)\b", re.I)
REPEAT = re.compile(r"\b(repeat|again|what|huh|come again|pardon|sorry|say that|one more time|slower)\b", re.I)
BACK = re.compile(r"\b(back|previous|last one|before that)\b", re.I)
STOP = re.compile(r"\b(stop|cancel|never ?mind|that'?s enough|i'?m good|enough|quit|no)\b", re.I)


WORDS = {"pad", "mag", "dot", "kit", "pro", "max", "mod", "red", "one", "two", "box", "fin", "tac", "cap",
         "rig", "jaw", "hex", "arc", "tri", "war"}            # short words, not letters to spell out
SLOTS = {"reargrip": "rear grip", "firemods": "fire mods", "underbarrel": "underbarrel"}


def speakable(name):
    """'17.6" LTI GRAV-4 BARREL' -> '17.6 inch LTI Grav-4 Barrel' (the voice spells out caps)."""
    def part(p):
        if any(c.isdigit() for c in p) or (len(p) <= 3 and p.lower() not in WORDS):
            return p                                     # LTI, SK, 17.6, GRAV-4's 4
        return p.capitalize()
    out = " ".join("-".join(part(p) for p in w.split("-")) for w in name.split())
    return re.sub(r'(\d)\s*"', r"\1 inch", out)


def slot_name(slot):
    return SLOTS.get(slot.lower(), slot.replace("_", " ").lower())


def _game(text, scene_game=""):
    for pat, gid in GAMES:
        if re.search(pat, text, re.I):
            return gid
    g = (scene_game or "").lower()
    for pat, gid in GAMES:
        if re.search(pat, g, re.I):
            return gid
    return "warzone"


def _mode(text):
    low = text.lower()
    return next((m for m in MODES if m in low), None)


def _category(text):
    return next((c for pat, c in CATEGORY if re.search(pat, text, re.I)), None)


def _gun_lines(w):
    b = w.get("build") or {}
    atts = [(slot_name(a.get("slot", "")), speakable(a.get("name", ""))) for a in b.get("attachments") or []]
    return speakable(w.get("name", "")), w.get("type", ""), atts


class Loadouts:
    def __init__(self):
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(6, connect=3))   # no oMLX key on this one
        self.offer = None        # steps waiting for a yes to "go through it slow?"
        self.steps, self.i = [], 0

    async def aclose(self):
        await self.http.aclose()

    @property
    def active(self):
        return bool(self.steps) or self.offer is not None

    def reset(self):
        self.offer, self.steps, self.i = None, [], 0

    async def _get(self, path, **params):
        r = await self.http.get(API + path, params={k: v for k, v in params.items() if v})
        r.raise_for_status()
        return r.json()

    async def answer(self, text, scene_game=""):
        """The class (and the steps for a slow read), or None if this isn't a class question."""
        if not CLASS_Q.search(text):
            return None
        game, mode, cat = _game(text, scene_game), _mode(text), _category(text)
        guns = []
        m = WEAPON_Q.search(text)
        if m and not re.fullmatch(r"(warzone|resurgence|ranked|multiplayer|me|it|this|that)", m["w"].strip(), re.I):
            hits = [h for h in (await self._get("/search", q=m["w"].strip(), game=game)).get("results") or []
                    if h.get("score", 0) >= 0.6]
            if not hits:                                  # not in this game: any game that has it
                hits = [h for h in (await self._get("/search", q=m["w"].strip())).get("results") or []
                        if h.get("score", 0) >= 0.6]
                if hits:
                    game = hits[0].get("game", game)
            if hits:
                d = await self._get(f"/games/{game}/weapons/{hits[0]['weapon_id']}", mode=mode)
                builds = d.get("builds") or []
                if builds:
                    guns = [{"name": d.get("name"), "type": d.get("type"), "build": builds[0]}]
        if not guns and cat:
            guns = (await self._get(f"/games/{game}/meta", mode=mode, category=cat, limit=1)).get("meta") or []
        if not guns:          # a full class: best long-range + best close-range
            for c in ("long-range", "close-range"):
                try:
                    guns += (await self._get(f"/games/{game}/meta", mode=mode, category=c, limit=1)).get("meta") or []
                except httpx.HTTPStatusError:
                    pass
            if not guns:
                guns = (await self._get(f"/games/{game}/meta", mode=mode, limit=2)).get("meta") or []
        if not guns:
            return "The meta site has nothing for that one.", []
        info = await self._get(f"/games/{game}")
        mname = next((x["name"] for x in info.get("modes", []) if x["id"] == mode or x["name"].lower() == (mode or "")),
                     (info.get("modes") or [{}])[0].get("name", ""))
        say, steps = [f"{info.get('name', game)} {mname}:" if mname else f"{info.get('name', game)}:"], []
        for n, w in enumerate(guns):
            gun, typ, atts = _gun_lines(w)
            role = ("Primary" if n == 0 else "Secondary") if len(guns) > 1 else "Run"
            say.append(f"{role}, the {gun}" + (f", {typ.lower()}" if typ else "") + ". "
                       + (", ".join(a if a.lower().endswith(s.split()[-1]) else f"{a} {s}" for s, a in atts) + "."
                          if atts else "No attachments listed."))
            steps.append(f"{role}, the {gun}.")
            steps += [f"{s.capitalize()}: {a}." for s, a in atts]
        say.append("Want me to go through it slow, one by one?")
        self.reset()
        self.offer = steps
        return " ".join(say), steps

    def follow(self, text):
        """While a class is on the table: the reply to 'yes' / 'next' / 'repeat' / 'back' / 'stop',
        or None to let the question go to the model as usual (and drop the class)."""
        t = text.strip()
        if self.offer is not None:
            steps, self.offer = self.offer, None
            if YES.search(t) and not NO.search(t):
                self.steps, self.i = steps, 0
                return f"Aight, slow. {self._step()}"
            if NO.search(t):
                return "Aight, go cook."
            return None
        if not self.steps:
            return None
        if STOP.search(t) and not NEXT.search(t.replace("no", "")):
            self.reset()
            return "Aight, stopping there."
        if REPEAT.search(t):
            return self._step()
        if BACK.search(t):
            self.i = max(0, self.i - 1)
            return self._step()
        if NEXT.search(t) or len(t.split()) <= 2:
            self.i += 1
            if self.i >= len(self.steps):
                self.reset()
                return "That's the whole class. Go cook."
            return self._step()
        self.reset()
        return None

    def _step(self):
        last = self.i == len(self.steps) - 1
        return f"{self.steps[self.i]} {'Last one. Got it?' if last else 'Got it?'}"
