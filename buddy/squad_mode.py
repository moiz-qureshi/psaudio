"""Squad mode: Hasaan as an always-on member of the party chat.

The SQUAD key turns it on. While on, Hasaan:
  - hears both sides: your headset mic ("Moiz") and the PS5's incoming party mix
    ("Squad", which also carries game sound), cut into utterances by voice activity
    and transcribed by Whisper on the MacBook;
  - sees your PS5 screen: the Mac's scene watcher (ps5_scene.py, its own process next to
    the PS5 Stream app) reads every changed frame with Qwen3-VL and keeps the current
    facts + a 15-minute event timeline (deaths, kills, spectating, frozen picture = AFK);
    this only reads that, so the picture is fresh no matter what Hasaan is doing;
  - decides on its own when to talk: right away when someone talks to it, after a
    pause when it has something to add, when the screen shows a moment worth a line
    (a death, a win, someone AFK or being spectated), with cool-downs so it doesn't
    spam; it speaks in the profile's voice to the headset and the PS5 mic.

Everything is local (oMLX on the Mac), so it costs nothing per hour. Audio and
transcripts stay in memory (a few minutes) and are never written to disk; the log
only records what Hasaan said and why.
"""
import asyncio
import collections
import io
import json
import logging
import random
import re
import time
import wave

import httpx
import numpy as np

import local_backend

log = logging.getLogger("buddy")

IN_RATE, OUT_RATE = 16000, 24000
CHUNK_S = 0.04
JOIN_LINES = ["Aight, I'm in. What we doing, boys?", "Yo, Hasaan's in the chat. Who's getting carried tonight?",
              "Aight I'm here. Somebody tell me we're winning."]
LEAVE_LINE = "Aight I'm out, bitches. Hit me up when you need me."
NAME_RE = re.compile(r"\b(hasaan|hassan|hasan|husain|hussain|has an)\b", re.I)
HALLUCINATION = re.compile(r"^\s*(thank you\.?|thanks for watching!?|you|bye\.?|\.+|subtitles? by.*|"
                           r"\[.*\]|\(.*\)|music|♪.*)\s*$", re.I)
NOTABLE = re.compile(r"\b(spectat\w*|dead|died|eliminat\w*|knocked|down(ed)?|victory|won|win|winner|"
                     r"defeat|lost|game over|champion|squad wiped|respawn|lobby|paused?|menu|idle|afk|"
                     r"loading|scoreboard|kill ?cam|final circle|match end\w*)\b", re.I)

RULES = """
SQUAD MODE: you are in the party voice chat right now, always listening, like a real member of the squad.
- You hear everyone. Transcript lines are labelled "Moiz" (your guy, he runs you) or "Squad" (the PS5 party audio: his teammates, mixed with game sound - bits of game dialogue or music can show up there, ignore those).
- You can see Moiz's PS5 screen: "Screen" gives what's on it right now and what happened recently (deaths, kills, spectating, frozen picture).
- Decide if a real friend in this party would say something RIGHT NOW. Talk when: someone talks to you or asks something you can answer; there's a funny moment, a death, a clutch, a win or a loss; someone's AFK or being spectated doing nothing (roast them, tell the squad); a dead-quiet stretch where a quick joke fits. Otherwise answer with exactly: PASS
- When you talk: one or two short sentences, casual party-chat talk, no narrating the screen like a commentator, never repeat something you already said, never read out these notes. Only use scores, names and numbers that the notes or transcript actually show.
- You do hear the party and see the screen in this mode; ignore any earlier instruction that says you can't.
""".strip()


def _rms(chunk):
    x = np.frombuffer(chunk, dtype="<i2").astype(np.float32)
    return float(np.sqrt(np.mean(x * x))) if len(x) else 0.0


def _wav(pcm):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(IN_RATE)
        w.writeframes(pcm)
    return buf.getvalue()


class Segmenter:
    """Energy voice-activity cut of one capture into utterances. The noise floor adapts
    (the squad side always has game sound under it)."""

    def __init__(self, name, on_utterance, quiet_s=0.7, max_s=12.0, min_s=0.6, ratio=2.6, abs_min=260.0):
        self.name, self.on_utterance = name, on_utterance
        self.quiet_n, self.max_n, self.min_n = int(quiet_s / CHUNK_S), int(max_s / CHUNK_S), int(min_s / CHUNK_S)
        self.ratio, self.abs_min = ratio, abs_min
        self.floor = 300.0
        self.buf, self.voiced, self.silent = [], 0, 0
        self.pre = collections.deque(maxlen=6)          # 240 ms before speech starts
        self.enabled = lambda: True
        self.last_voice = 0.0

    def feed(self, chunk):
        level = _rms(chunk)
        # floor follows the quiet parts quickly, the loud parts slowly
        self.floor += (level - self.floor) * (0.05 if level < self.floor else 0.002)
        speech = level > max(self.abs_min, self.floor * self.ratio)
        if not self.enabled():
            self.buf, self.voiced, self.silent = [], 0, 0
            self.pre.clear()
            return
        if not self.buf:
            self.pre.append(chunk)
            if speech:
                self.buf, self.voiced, self.silent = list(self.pre), 1, 0
                self.pre.clear()
            return
        self.buf.append(chunk)
        if speech:
            self.voiced += 1
            self.silent = 0
            self.last_voice = time.monotonic()
        else:
            self.silent += 1
        if self.silent >= self.quiet_n or len(self.buf) >= self.max_n:
            buf, voiced = self.buf, self.voiced
            self.buf, self.voiced, self.silent = [], 0, 0
            if voiced >= self.min_n:
                self.on_utterance(self.name, b"".join(buf))


class SquadMode:
    def __init__(self, buddy):
        self.b = buddy
        self.cfg = buddy.cfg
        self.on = False
        self.task = None
        self.client = None
        self.lines = collections.deque(maxlen=80)      # (t, who, text, handled)
        self.dropped = collections.deque(maxlen=20)    # debug: what the Whisper filters threw away
        self.scene = {}                                 # latest /scene from the Mac
        self.scene_task = None
        self.last_event_ts = time.time()
        self.static_s = 0
        self.live = False
        self.last_said = 0.0
        self.last_unprompted = 0.0
        self.said = collections.deque(maxlen=8)
        self.wake = asyncio.Event()
        self.reason = None
        self.speaking = False
        self.cutting = False                           # STOP pressed: drop the rest of the line
        self.saying = None                             # the line being spoken right now
        self.deciding = False
        self.stt_busy = 0
        self.last_moiz = time.monotonic()
        self.stats = {"heard": 0, "spoke": 0, "passed": 0, "frames": 0}
        # A50 X mic (measured 2026-10-26): floor ~20 RMS, speech ~100-900; the squad side
        # always has game sound under it, so it keeps a higher bar
        self.segs = {"mic": Segmenter("mic", self._utterance, abs_min=60.0, ratio=3.0, min_s=0.35),
                     "squad": Segmenter("squad", self._utterance, quiet_s=0.6)}
        self.segs["squad"].enabled = self._squad_ok

    # -- config ----------------------------------------------------------------------
    @property
    def conf(self):
        return self.cfg.get("squad", {})

    def profile(self):
        p = self.b.profile
        if p["kind"] == "local":
            return p
        for q in self.b.profiles.list():
            if q.get("kind") == "local":
                return q
        return None

    def status(self):
        return {"on": self.on, "speaking": self.speaking, "video": self.live, "static_s": self.static_s,
                "last_note": (self.scene or {}).get("summary") if self.live else None, "said": list(self.said)[-3:], **self.stats}

    # -- on / off --------------------------------------------------------------------
    async def start(self):
        if self.on:
            return
        p = self.profile()
        if not p:
            await self.b.set_state("error", "squad mode needs a local (MacBook) profile")
            return
        self.p = p
        self.on = True
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(20, connect=4),
                                        headers={"Authorization": f"Bearer {self.cfg.get('omlx_key', '')}"})
        self.base = p["base_url"].rstrip("/")
        self.lines.clear()
        self.last_moiz = time.monotonic()
        if not self.b.session_on:
            await self.b.session_start()          # the AI key and "Hey Hasaan" keep working too
        for name, mic in (("mic", self.b.mic), ("squad", self.b.squad)):
            if self.segs[name].feed not in mic.taps:
                mic.taps.append(self.segs[name].feed)
            await mic.start()
        self.task = asyncio.create_task(self._brain())
        self.ensure_scene()
        log.info("squad mode on (voice %s, video %s)", p["id"], self.conf.get("frame_url"))
        self.b.bump()
        asyncio.create_task(self.say(random.choice(JOIN_LINES), why="joined"))
        asyncio.create_task(self._prepare_leave())

    async def stop(self, sign_off=True):
        if not self.on:
            return
        self.on = False
        for name, mic in (("mic", self.b.mic), ("squad", self.b.squad)):
            if self.segs[name].feed in mic.taps:
                mic.taps.remove(self.segs[name].feed)
        if self.task:
            self.task.cancel()
        self.task = None
        if sign_off:
            if not self._play_clip(LEAVE_LINE):
                await self.say(LEAVE_LINE, why="left", force=True)
        if self.client:
            await self.client.aclose()
            self.client = None
        self.lines.clear()
        log.info("squad mode off (heard %d, spoke %d, passed %d, frames %d)", self.stats["heard"],
                 self.stats["spoke"], self.stats["passed"], self.stats["frames"])
        self.b.bump()

    async def _prepare_leave(self):
        p = self.profile()
        if p and not self.b.clip_path(LEAVE_LINE, p).exists():
            await self.b._render_clip(p, LEAVE_LINE)

    def _play_clip(self, text):
        path = self.b.clip_path(text, self.p)
        if not path.exists():
            return False
        with wave.open(str(path)) as w:
            pcm = w.readframes(w.getnframes())
        return self.b.player.push(pcm)

    # -- ears --------------------------------------------------------------------------
    def _busy(self):
        """The normal AI key / wake word owns the moment (it will answer itself). The
        player flags the buddy "speaking" for squad mode's own lines too: that one doesn't count."""
        if self.b.listening or self.b.state in ("listening", "thinking"):
            return True
        return self.b.state == "speaking" and not self.speaking

    def _squad_ok(self):
        """The party side keeps being heard while Hasaan talks; what comes back of his
        own voice through friends' open mics is dropped after transcription (_echo)."""
        return True

    def _echo(self, text):
        """True if this party line is just Hasaan's own words coming back."""
        import difflib
        if time.monotonic() - self.last_said > 4 and not self.speaking:
            return False
        words = re.findall(r"\w+", text.lower())
        for said in list(self.said)[-2:] + ([self.saying] if self.saying else []):
            sw = re.findall(r"\w+", said.lower())
            if not words or not sw:
                continue
            m = difflib.SequenceMatcher(None, words, sw).find_longest_match(0, len(words), 0, len(sw))
            if m.size >= 4 or (m.size >= 2 and m.size >= 0.6 * len(words)):
                return True
        return False

    def _utterance(self, name, pcm):
        if not self.on:
            return
        if name == "mic":
            self.last_moiz = time.monotonic()
        self.b.last_use = time.monotonic()        # keeps the AI session from timing out
        if self._busy():
            return       # the AI key / wake word owns this one: keep the Mac's GPU for its answer
        asyncio.create_task(self._transcribe(name, pcm, False))

    async def _transcribe(self, name, pcm, handled):
        self.stt_busy += 1
        try:
            form = {"model": self.p["stt"], "response_format": "verbose_json", "language": self.p.get("language") or "en",
                    "prompt": "Gaming squad party chat. Hasaan is the AI. Names: "
                    + (getattr(self.b.session, "vocab", None) or "Hasaan")}
            r = await self.client.post(self.base + "/audio/transcriptions",
                                       files={"file": ("u.wav", _wav(pcm), "audio/wav")}, data=form)
            r.raise_for_status()
            d = r.json()
        except Exception as e:
            log.debug("squad stt failed: %s", e)
            return
        finally:
            self.stt_busy -= 1
        segs = [s for s in d.get("segments") or [] if s.get("avg_logprob", 0) > -1.0
                and s.get("compression_ratio", 1) < 2.4 and s.get("no_speech_prob", 0) < 0.6]
        text = " ".join(s.get("text", "").strip() for s in segs).strip() if d.get("segments") else d.get("text", "").strip()
        if not text or HALLUCINATION.match(text) or len(text) < 2:
            self.dropped.append({"who": name, "raw": (d.get("text") or "")[:120],
                                 "seg": [(round(x.get("avg_logprob", 0), 2), round(x.get("no_speech_prob", 0), 2))
                                         for x in (d.get("segments") or [])][:4]})
            return
        if name == "squad" and self._echo(text):
            self.dropped.append({"who": name, "raw": text[:120], "seg": "echo of Hasaan"})
            return
        who = "Moiz" if name == "mic" else "Squad"
        self.lines.append((time.monotonic(), who, text, handled))
        self.stats["heard"] += 1
        if handled:
            return
        if NAME_RE.search(text):
            self._trigger("addressed")
        elif text.rstrip().endswith("?"):
            self._trigger("question", delay=1.2)
        else:
            self._trigger("talk", delay=float(self.conf.get("quiet_s", 4.0)))

    # -- eyes: the Mac's scene watcher (ps5_scene.py) looks nonstop; this just reads it ----
    def ensure_scene(self):
        """Poll the scene while the AI session is on (squad mode or not): the AI key's
        answers use it too. A few ms per poll; the looking happens on the Mac."""
        if self.scene_task is None or self.scene_task.done():
            self.scene_task = asyncio.create_task(self._scene())

    async def _scene(self):
        url = self.conf.get("scene_url", "http://192.168.4.38:8899").rstrip("/") + "/scene"
        h = {"Authorization": f"Bearer {self.cfg.get('ps5_stream_token', '')}"}
        async with httpx.AsyncClient(timeout=httpx.Timeout(2, connect=1)) as c:
            while self.b.session_on or self.on:
                await asyncio.sleep(1.0)
                try:
                    sc = (await c.get(url, headers=h)).json()
                except Exception:
                    self.live, self.scene = False, {}
                    continue
                self.scene, self.live = sc, bool(sc.get("live"))
                self.static_s = int(sc.get("static_s") or 0)
                now = time.time()
                for e in sc.get("events") or []:
                    ts = now - e["ago_s"]
                    if ts <= self.last_event_ts + 1.5:
                        continue
                    self.last_event_ts = ts
                    if not self.on or e["ago_s"] > 10:
                        continue
                    if "frozen" in e["text"]:
                        self._trigger("afk", delay=0.3)
                    elif re.search(r"died|spectating|kill|scoreboard|score|won|lost|wiped|win|match", e["text"], re.I):
                        self._trigger("screen", delay=0.3)
        self.scene_task = None

    def scene_lines(self, last_s=180):
        if not self.live or not self.scene:
            return []
        out = [f"Right now ({self.scene.get('age_s', '?')}s ago): {self.scene.get('summary') or 'no description yet'}"]
        out += [f"[{e['ago_s']}s ago] {e['text']}" for e in self.scene.get("events") or [] if e["ago_s"] < last_s][-10:]
        return out

    # -- brain -------------------------------------------------------------------------
    def _trigger(self, reason, delay=0.0):
        prio = {"addressed": 3, "afk": 2, "screen": 2, "question": 1, "talk": 0}
        if self.reason is None or prio.get(reason, 0) >= prio.get(self.reason[0], 0):
            self.reason = (reason, time.monotonic() + delay)
        self.wake.set()

    def _quiet_for(self):
        last = max([s.last_voice for s in self.segs.values()] + [self.last_said])
        return time.monotonic() - last

    async def _brain(self):
        idle_every = float(self.conf.get("idle_comment_s", 240))
        while self.on:
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
            self.wake.clear()
            now = time.monotonic()
            if self.reason is None:
                if self.live and now - max(self.last_said, self.last_unprompted) > idle_every and self._quiet_for() > 20:
                    self.reason = ("idle", now)
                else:
                    continue
            reason, at = self.reason
            if now < at:
                # wait out the pause; new speech pushes it back
                await asyncio.sleep(min(0.5, at - now))
                self.wake.set()
                continue
            if reason in ("talk", "question") and self._quiet_for() < float(self.conf.get("quiet_s", 4.0)) * 0.5:
                self.reason = (reason, now + 1.0)          # people are still talking
                self.wake.set()
                continue
            if self._busy() or self.speaking or self.b.player.busy() or self.deciding:
                await asyncio.sleep(0.3)                   # keep it: answer right after
                self.wake.set()
                continue
            self.reason = None
            unprompted = reason not in ("addressed",)
            if unprompted and now - self.last_unprompted < float(self.conf.get("cooldown_s", 20)):
                continue
            await self._decide(reason, unprompted)

    def _context(self, reason):
        now = time.monotonic()
        window = float(self.conf.get("context_s", 180))
        talk = [f"[{int(now - t)}s ago] {who}: {text}" for t, who, text, _ in self.lines if now - t < window]
        seen = self.scene_lines()
        mine = [f"- {s}" for s in self.said]
        moiz_quiet = int(now - self.last_moiz)
        why = {
            "addressed": "Someone just said your name - answer them.",
            "question": "Someone asked something. Answer only if it's for you or you can add something.",
            "talk": "The chat just went quiet. Chime in only if you have something good.",
            "screen": "Something changed on screen. React only if it's a moment worth a line.",
            "afk": self._afk_hint(moiz_quiet),
            "idle": "Nobody has talked for a while. Say something only if it fits.",
        }[reason]
        return "\n".join([
            "Party chat (oldest first):", *(talk or ["(nobody has said anything yet)"]), "",
            "Screen (Moiz's PS5, watched live):", *(seen or
                ["(no stream right now: you can't see the screen, so don't mention anything on it)"]),
            (f"(picture frozen for {self.static_s}s; Moiz last spoke {moiz_quiet}s ago)" if self.live
             else f"(Moiz last spoke {moiz_quiet}s ago)"), "",
            "What you said recently:", *(mine or ["(nothing yet)"]), "",
            why, "Reply with your line (no PASS this time)." if "Don't PASS" in why
            else "Reply with your line, or exactly PASS."])

    def _afk_hint(self, moiz_quiet):
        """The picture has been frozen: say who looks AFK, so the roast lands on the right guy."""
        now = (self.scene or {}).get("now") or {}
        note = " ".join(str(v) for v in now.values() if v)
        froze = f"The picture hasn't moved for {self.static_s}s."
        who = (now.get("spectating") or "").strip() if now.get("screen") == "spectating" else ""
        if who and who.lower() not in ("player", "the", "a", "screen", "mode"):
            return (f"{froze} Moiz is spectating {who}, who is just standing there: {who} is AFK. "
                    "Call it out to the squad and roast them for it now. Don't PASS.")
        if re.search(r"\b(loading|matchmaking|lobby|queue)\b", note, re.I):
            return f"{froze} It's a loading/lobby screen, so nobody's AFK. Only talk if it's been forever."
        if moiz_quiet > 60:
            return (f"{froze} Moiz hasn't said a word for {moiz_quiet}s either: the jackass is AFK. "
                    "Tell the squad and roast him for it now. Don't PASS.")
        return f"{froze} Moiz is still talking, so he's probably just in a menu. React only if it's funny."

    async def _decide(self, reason, unprompted):
        self.deciding = True
        try:
            system = self.b.personality() + "\n\n" + RULES
            msgs = [{"role": "system", "content": system}, {"role": "user", "content": self._context(reason)}]
            r = await self.client.post(self.base + "/chat/completions", json={
                "model": self.p["llm"], "messages": msgs, "max_tokens": 80,
                "temperature": float(self.p.get("temperature", 0.9)),
                "chat_template_kwargs": {"enable_thinking": False}})
            r.raise_for_status()
            text = local_backend.THINK.sub("", r.json()["choices"][0]["message"]["content"] or "").strip()
        except Exception as e:
            log.warning("squad decision failed: %s", e)
            return
        finally:
            self.deciding = False
        text = re.sub(r"^(hasaan|hassan)\s*:\s*", "", text, flags=re.I).strip().strip('"')
        text = re.sub(r"[\s.,;:!-]*\bPASS\b\W*$", "", text, flags=re.I).strip()     # "line... Pass." -> line
        if not text or re.fullmatch(r"\W*pass\W*", text, re.I) or text.upper().startswith("PASS"):
            self.stats["passed"] += 1
            return
        if not self.on or self._busy():
            return
        if unprompted:
            self.last_unprompted = time.monotonic()
        heard = next((f"{who}: {t}" for _, who, t, _ in reversed(self.lines) if not who.startswith("Hasaan")), "")
        await self.say(text, why=reason, heard=heard if reason in ("addressed", "question", "talk") else "")

    def cut(self):
        if self.speaking:
            self.cutting = True

    # -- mouth -------------------------------------------------------------------------
    async def say(self, text, why="", force=False, heard=""):
        if not force and (not self.on or self._busy()):
            return
        p = self.profile()
        if not p or not self.client:
            return
        self.speaking, self.saying = True, text
        self.b.question, self.b.reply = heard[-200:], text      # the deck's LAST REPLY panel
        self.b.bump()
        log.info("squad (%s): %r", why, text)
        pushed = 0
        try:
            held, started = bytearray(), False
            async for pcm in local_backend.tts_stream(self.client, self.base, p, text):
                if self.cutting or (not force and self._busy()):
                    break                              # STOP, or someone pressed the AI key
                if started:
                    self.b.player.push(pcm)
                    pushed += len(pcm)
                    continue
                held += pcm
                if len(held) >= OUT_RATE * 2 * local_backend.JITTER_S:
                    started = True
                    self.b.player.push(bytes(held))
                    pushed += len(held)
            if not started and held:
                self.b.player.push(bytes(held))
                pushed += len(held)
            self.stats["last_audio_s"] = round(pushed / (OUT_RATE * 2), 2)
            self.said.append(text)
            self.lines.append((time.monotonic(), "Hasaan (you)", text, True))
            self.stats["spoke"] += 1
            self.last_said = time.monotonic()
            # stay "speaking" until the player has played it out
            while self.b.player.busy():
                await asyncio.sleep(0.1)
            self.last_said = time.monotonic()
        except Exception as e:
            log.warning("squad speech failed: %s", e)
        finally:
            self.speaking = self.cutting = False
            self.saying = None
            self.b.bump()

    def name_only(self, source):
        """'Hey Hasaan' and then nothing: in the party that means 'you there?' - answer it."""
        if not self.on:
            return
        self.lines.append((time.monotonic(), "Moiz" if source == "mic" else "Squad", "Hey Hasaan?", False))
        self._trigger("addressed")

    def recent_chat(self, seconds=90):
        """For the AI key's answers: the live screen (whenever the stream is on) and, in
        squad mode, what was just said in the party."""
        now = time.monotonic()
        rows = [f"{who}: {text}" for t, who, text, _ in self.lines if now - t < seconds][-12:] if self.on else []
        seen = self.scene_lines(last_s=120)
        if not rows and not seen:
            return ""
        out = []
        if seen:
            out.append("(You can see Moiz's PS5 screen live. Use it if they ask what's going on; don't "
                       "invent anything that isn't here:\n" + "\n".join(seen) + ")")
        if rows:
            out.append("(Squad mode is on: you hear the party. Just said:\n" + "\n".join(rows) + ")")
        return "\n".join(out)
