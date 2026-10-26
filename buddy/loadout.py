"""Full classes from the wzmeta API, read out once, then (if wanted) one piece at a time.

  "what's the meta class for resurgence?"  -> long-range + close-range weapon with every
                                              attachment, then "Want me to go through it slow?"
  "yes"                                    -> "Primary, the VOYAK KT-3. Got it?"
  "next" / "got it" / "ok"                 -> the next piece        ("repeat", "back", "stop" work too)
  "mosin nagant class" / "best m4 build"   -> that gun's recommended build (fuzzy on the name)
  "send it" / "PSN"                        -> the class as text, through crcmz, to the WhatsApp
                                              group or the PSN squad group (never a DM)

Answers are built here, not by the model: names have to be exact and it is faster.
API: https://wzmeta.interestingsoup.com/api (games mw4, warzone, bo6, bo7).
"""
import re
import time
from difflib import SequenceMatcher

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

YES = re.compile(r"\b(yes|yeah|yep|yup|ya|sure|ok(?:ay)?|please|go ahead|do it|bet|alright|slow(?:er|ly)?|"
                 r"one by one|step by step|repeat|again|go through|break it down)\b", re.I)
NO = re.compile(r"^\W*(no|nah|nope|i'?m good|all good|never ?mind|stop|cancel)\b", re.I)
NEXT = re.compile(r"\b(next|got it|gotcha|ok(?:ay)?|yes|yeah|yep|yup|done|go|continue|ready|cool|alright|"
                  r"sure|k|keep going|go on|and)\b", re.I)
REPEAT = re.compile(r"\b(repeat|again|what|huh|come again|pardon|sorry|say that|one more time|slower)\b", re.I)
BACK = re.compile(r"\b(back|previous|last one|before that)\b", re.I)
SEND = re.compile(r"\b(whats ?app|psn|play ?station|send|text|message|dm|drop it|post)\b", re.I)
PSN = re.compile(r"\b(psn|play ?station)\b", re.I)
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


WEAPON_TYPES = [(r"\bshot ?guns?\b", "shotgun"), (r"\blmgs?\b|light machine", "lmg"),
                (r"\bmarksman", "marksman-rifle"), (r"\bpistols?\b|\bhand ?guns?\b", "pistol"),
                (r"\bsmgs?\b|sub ?machine", "smg"), (r"\bsnipers?\b", "sniper"),
                (r"\bars?\b|assault rifles?", "assault-rifle")]


def _wtype(text):
    return next((t for pat, t in WEAPON_TYPES if re.search(pat, text, re.I)), None)


def _category(text):
    return next((c for pat, c in CATEGORY if re.search(pat, text, re.I)), None)


# Words in a class question that are never part of a gun's name (so "best" or "class"
# can't fuzzy-match a gun that starts with those letters).
NOT_GUN = {"what", "whats", "the", "best", "meta", "class", "classes", "build", "builds", "loadout",
           "loadouts", "load", "out", "attachments", "attachment", "setup", "for", "on", "in", "give",
           "me", "my", "a", "an", "of", "tell", "about", "gun", "weapon", "rifle", "smg", "sniper",
           "warzone", "resurgence", "ranked", "multiplayer", "dmz", "pvp", "pve", "mw4", "bo6", "bo7",
           "long", "close", "short", "range", "ranged", "fast", "quick", "good", "top", "new", "type",
           "shotgun", "lmg", "marksman", "pistol", "launcher", "assault", "ar"}
GUN_LIST_TTL = 6 * 3600


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def find_gun(text, guns):
    """The gun named in `text`, or None. `guns` = [(weapon_id, name)].

    Matches any phrasing ("mosin nagant class", "best build for the m4") against the real
    names, glued together so "kt3" / "kt 3" / "KT-3" all match, and forgives the speech
    recogniser's spelling ("mohsin nagant", "mozin") on names of 5+ letters. Short names
    (M4, HDR) only match exactly, so "mw4" never becomes the M4."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    best, best_score = None, 0.0
    for wid, name in guns:
        toks = re.findall(r"[a-z0-9]+", name.lower()) or [""]
        n, k = _norm(name), len(toks)
        for size in sorted({k, k + 1, max(1, k - 1)}):
            for i in range(len(words) - size + 1):
                span = words[i:i + size]
                cand = "".join(span)
                if cand == n:
                    score = 1.0 + len(n) / 100          # exact ("type 73", "m4"); longer names win ties
                elif span[0] in NOT_GUN or span[-1] in NOT_GUN:
                    continue
                elif len(cand) >= 4 and k > 1 and cand == "".join(toks[:size]):
                    score = 0.88                        # the name's first word(s): "ppsh", "mosin", "kastov"
                elif len(n) >= 5 and len(cand) >= 5:
                    score = SequenceMatcher(None, cand, n).ratio()   # "mohsin nagant", "mozin nagant"
                else:
                    continue
                if score > best_score:
                    best, best_score = (wid, name), score
    return best if best_score >= 0.82 else None


def _best_build(builds, mode):
    """The build wzstats recommends for this mode, else the first one for it, else any."""
    for b in builds:
        if mode and mode in (b.get("best_for_modes") or []):
            return b
    for b in builds:
        if b.get("best_for_modes"):
            return b
    return builds[0] if builds else None


def _gun_lines(w):
    b = w.get("build") or {}
    atts = [(slot_name(a.get("slot", "")), speakable(a.get("name", ""))) for a in b.get("attachments") or []]
    return speakable(w.get("name", "")), w.get("type", ""), atts


class Loadouts:
    def __init__(self):
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(6, connect=3))   # no oMLX key on this one
        self.offer = None        # steps waiting for a yes to "go through it slow?"
        self.steps, self.i = [], 0
        self.written = []        # the class as text messages (<= 500 chars each), for "send it"
        self._guns = {}          # game -> (fetched_at, [(weapon_id, name)])

    async def aclose(self):
        await self.http.aclose()

    @property
    def active(self):
        return bool(self.steps) or self.offer is not None or bool(self.written)

    def reset(self):
        self.offer, self.steps, self.i, self.written = None, [], 0, []

    async def _get(self, path, **params):
        r = await self.http.get(API + path, params={k: v for k, v in params.items() if v})
        r.raise_for_status()
        return r.json()

    async def guns(self, game):
        """Every gun the game ranks in any mode, cached for a few hours."""
        hit = self._guns.get(game)
        if hit and time.monotonic() - hit[0] < GUN_LIST_TTL:
            return hit[1]
        info = await self._get(f"/games/{game}")
        seen = {}
        for m in info.get("modes") or []:
            for w in (await self._get(f"/games/{game}/tier-list", mode=m["id"])).get("weapons") or []:
                seen.setdefault(w["weapon_id"], w["name"])
        self._guns[game] = (time.monotonic(), list(seen.items()))
        return self._guns[game][1]

    async def answer(self, text, scene_game="", can_send=False):
        """The class (and the steps for a slow read), or None if this isn't a class question."""
        if not CLASS_Q.search(text):
            return None
        game, mode, cat = _game(text, scene_game), _mode(text), _category(text)
        guns, note = [], ""
        hit = find_gun(text, await self.guns(game))
        if not hit:                                       # not in this game: any game that has it
            for other in ("mw4", "warzone", "bo7", "bo6"):
                if other != game and (hit := find_gun(text, await self.guns(other))):
                    game = other
                    break
        # The spoken mode as this game's mode id ("dmz pvp" -> dmz-pvp); one it doesn't have is dropped
        info = await self._get(f"/games/{game}")
        modes = info.get("modes") or []
        ids = {_norm(x): m["id"] for m in modes for x in (m["id"], m["name"])}
        if mode and _norm(mode) not in ids:
            note = f"{info.get('name', game)} has no {mode}, so here's {(modes or [{}])[0].get('name', 'the main mode')}. "
        mode = ids.get(_norm(mode)) if mode else None
        if hit:
            d = await self._get(f"/games/{game}/weapons/{hit[0]}", mode=mode)
            b = _best_build(d.get("builds") or [], mode or (modes or [{}])[0].get("id"))
            if not b:
                return f"The meta site has no build for the {speakable(d.get('name', hit[1]))} yet.", []
            guns = [{"name": d.get("name"), "type": d.get("type"), "build": b}]
            if mode and mode not in (b.get("modes") or []) and b.get("modes"):
                names = {m["id"]: m["name"] for m in modes}
                note = f"No {names.get(mode, mode)} build for that one, here's the {names.get(b['modes'][0], b['modes'][0])} one. "
                mode = b["modes"][0]
        else:
            m = WEAPON_Q.search(text)                     # "class for the <something we don't know>"
            if m and not re.fullmatch(r"(warzone|resurgence|ranked|multiplayer|me|it|this|that|"
                                      r"smgs?|ars?|snipers?|shotguns?|lmgs?|long range|close range)",
                                      m["w"].strip(), re.I):
                return f"Couldn't find a gun called {m['w'].strip()} on the meta site.", []
        wtype = _wtype(text)
        if not guns and wtype:                            # "best shotgun", "marksman rifle class"
            try:
                guns = (await self._get(f"/games/{game}/meta", mode=mode, type=wtype, limit=1)).get("meta") or []
            except httpx.HTTPStatusError:                 # a type this game doesn't have
                guns = []
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
        mname = next((x["name"] for x in info.get("modes", []) if x["id"] == mode or x["name"].lower() == (mode or "")),
                     (info.get("modes") or [{}])[0].get("name", ""))
        title = f"{info.get('name', game)} {mname}".strip()
        say, steps, written = [f"{note}{title}:"], [], []
        for n, w in enumerate(guns):
            gun, typ, atts = _gun_lines(w)
            role = ("Primary" if n == 0 else "Secondary") if len(guns) > 1 else "Run"
            say.append(f"{role}, the {gun}" + (f", {typ.lower()}" if typ else "") + ". "
                       + (", ".join(a if a.lower().endswith(s.split()[-1]) else f"{a} {s}" for s, a in atts) + "."
                          if atts else "No attachments listed."))
            steps.append(f"{role}, the {gun}.")
            steps += [f"{s.capitalize()}: {a}." for s, a in atts]
            b = w.get("build") or {}
            written.append(f"{role}: {w.get('name', '')}" + (f" ({typ})" if typ else "") + "\n" + "\n".join(
                f"- {slot_name(a.get('slot', '')).capitalize()}: {a.get('name', '')}" for a in b.get("attachments") or []))
        say.append("Want me to go through it slow, or send it to the WhatsApp or PSN group?" if can_send
                   else "Want me to go through it slow, one by one?")
        self.reset()
        self.offer = steps
        whole = f"{title} class (wzmeta)\n\n" + "\n\n".join(written)
        self.written = [whole] if len(whole) <= 490 else [f"{title} class (wzmeta)\n{x}"[:490] for x in written]
        return " ".join(say), steps

    async def _send(self, t, asker, send, names, me):
        """'send it' / 'the group' / 'PSN'. Classes only ever go to a group chat, never a DM:
        on party audio a friend's voice can't be told apart from yours, so 'send it to me'
        could land in the wrong person's WhatsApp. PSN if they said PSN, else the WhatsApp group."""
        if PSN.search(t):
            tool, args, where = "send_psn_group_message", {}, "the PSN group"
        else:
            tool, args, where = "send_whatsapp_group_message", {}, "the WhatsApp group"
        for msg in self.written:
            res = str(await send(tool, dict(args, message=msg)))
            if res.startswith("error") or re.search(r'"(ok|success)":\s*false|"error":\s*"', res[:400]):
                why = re.search(r'"(?:error|detail|message)":\s*"([^"]{1,90})', res)
                return f"Couldn't send it to {where}: {why.group(1) if why else res[:80]}."
        self.reset()
        return f"Sent it to {where}."

    async def follow(self, text, asker="mic", send=None, names=(), me=""):
        """While a class is on the table: the reply to 'yes' / 'next' / 'repeat' / 'back' / 'stop' /
        'send it', or None to let the question go to the model as usual (and drop the class)."""
        t = text.strip()
        if self.written and send and SEND.search(t) and not NO.search(t):
            return await self._send(t, asker, send, names, me)
        if self.offer is None and not self.steps:
            self.reset()                      # only the text was left: anything else drops it
            return None
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
