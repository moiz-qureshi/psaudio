#!/usr/bin/env python3
"""psaudio-buddy: a push-to-talk Gemini Live voice for the PS Audio Bridge.

The headset mic is copied (never moved) into a Gemini Live session while the
AI BUDDY key is held; the spoken reply is played into both the A50 X headset
sink and the PS5 microphone sink, next to the existing game-audio loopback,
mic-effects stream and soundboard. The service only ever adds its own
streams to the PipeWire graph, so if it stops, crashes or loses the internet
the rest of the bridge carries on untouched.

Control + status is a small HTTP API (bearer token + client allowlist):

  POST /press  /release      hold-to-talk (a quick tap toggles instead)
  POST /talk/start /talk/end explicit start / submit
  POST /stop                 cut playback, drop queued audio, cancel the turn
  POST /arm /disarm /toggle  keep a Live session ready (idle timeout disarms)
  POST /volume?set=N|up=N|down=N
  GET  /status[?since=SEQ&wait=25]   long-poll for state changes
  GET  /health               no auth; liveness only
"""
import asyncio
import base64
import collections
import datetime
import json
import logging
import os
import pathlib
import signal
import subprocess
import sys
import time
import queue as queue_mod
import re
import threading
import tomllib

import numpy as np
from aiohttp import web

try:
    from google import genai
    from google.genai import errors as genai_errors
    from google.genai import types
except ImportError:  # the control API still answers; every turn reports the error
    genai = None

log = logging.getLogger("buddy")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import local_backend  # noqa: E402
import mcp_client  # noqa: E402

CONFIG_PATH = pathlib.Path(os.environ.get(
    "BUDDY_CONFIG", "~/.config/psaudio-buddy/buddy.toml")).expanduser()
STATE_DIR = pathlib.Path(os.environ.get(
    "BUDDY_STATE", "~/.local/state/psaudio-buddy")).expanduser()

DEFAULTS = {
    "model": "gemini-3.8-live",
    "voice": "Puck",
    "ai_volume": 70,             # percent, applied before the limiter
    "limiter_ceiling": 0.85,     # peak of the AI signal after limiting (≈ -1.4 dBFS)
    "idle_timeout_s": 900,       # an armed session closes after this long unused
    "max_listen_s": 30,          # a stuck key submits after this long
    "hold_threshold_s": 0.35,    # released sooner than this = tap (toggle mode)
    "max_reply_s": 25,           # playback is cut (and the turn cancelled) past this
    "max_output_tokens": 1200,
    "ack_tts_model": "gemini-3.8-flash-tts",   # renders the wake reply in a Gemini voice
    "preroll_ms": 300,           # audio kept (memory only) from just before the press / wake word
    "context_trigger_tokens": 6000,   # sliding-window compression bounds history
    "system_instruction": (
        "You are the squad's AI gaming buddy. Speak casually and naturally. "
        "Default to one or two short sentences. Be witty when appropriate and "
        "useful when asked. Do not claim to see gameplay, hear the squad, or "
        "know live match events unless that information was explicitly "
        "supplied. Avoid long introductions. You are an AI teammate, not a "
        "real person. Your name is Hasaan; questions may start with "
        "'Hey Hasaan'. Several squad members may talk to you."),
    "mic_source": "alsa_input.usb-Logitech_A50_X-00.mono-fallback",
    "headset_sink": "alsa_output.usb-Logitech_A50_X-00.analog-stereo",
    "ps5_mic_sink": "alsa_output.platform-fe980000.usb.mono-fallback",
    "listen": "0.0.0.0",
    "port": 8766,
    "allow": ["127.0.0.1", "::1", "192.168.5.54"],
    "wake": {
        "enabled": True,          # "Hey Hasaan" while AI SESSION is on
        "phrases": ["hey hassan", "hey hasan"],
        "mic": True,              # your headset mic
        "squad": True,            # the PS5's incoming mix (party chat + game)
        "squad_source": "alsa_input.platform-fe980000.usb.stereo-fallback",
        "model": "~/.local/lib/psaudio-buddy/models/vosk-model-small-en-us-0.15",
        "no_speech_timeout_s": 4,     # wake word but no question: give up
        "end_silence_ms": 600,        # your mic: this much quiet after words ends the question
        "squad_max_listen_s": 12,
        "squad_holdoff_s": 1.5,       # squad wake ignored while / just after the AI speaks
        "chime": True,
        "session_off_after_s": 7200,  # AI SESSION switches itself off after this long unused
    },
    "limits": {
        "max_turns_per_day": 300,
        "max_audio_in_s_per_day": 3600,
        "max_estimated_usd_per_day": 1.00,
    },
    # USD per 1M tokens, from ai.google.dev/gemini-api/docs/pricing (paid tier).
    # Used only for the *estimate* shown in /status — billing is what Google says.
    "price_per_mtok": {"in_audio": 3.00, "in_text": 0.75,
                       "out_audio": 12.00, "out_text": 4.50},
}

# The default personality (editable on Deck Manager's AI page; stored in profiles.json)
PERSONALITY = (
    "You are Hasaan, the squad's foul-mouthed AI gaming buddy. You're hilarious, cocky and "
    "quick: roast people, talk trash, and swear casually and often (fuck, shit, damn, hell, "
    "ass) the way friends do in party chat. Keep it to one or two punchy sentences and land the "
    "joke fast, with no long setups or introductions. When someone actually asks for help, give "
    "a real, useful answer, just with attitude. Never use slurs or mock anyone's race, religion, "
    "sexuality, gender or disability; roast their aim, skills and decisions instead. Don't claim "
    "to see the game, hear the squad, or know live match events unless someone tells you. "
    "Questions may start with 'Hey Hasaan', and several squad members may talk to you. You're "
    "an AI teammate, not a real person, and you own it.")

IN_RATE, OUT_RATE = 16000, 24000          # Gemini Live: 16 kHz in, 24 kHz out, s16le mono
IN_CHUNK = IN_RATE * 2 * 40 // 1000       # 40 ms of mic per message
OUT_FRAME = OUT_RATE * 2 * 20 // 1000     # playback is fed in 20 ms frames
PLAY_LEAD_S = 0.08                        # never more than this written ahead of real time


def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, "rb") as f:
            user = tomllib.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    cfg["api_key"] = os.environ.get("GEMINI_API_KEY", "").strip()
    cfg["token"] = os.environ.get("BUDDY_TOKEN", "").strip()
    cfg["omlx_key"] = os.environ.get("OMLX_API_KEY", "").strip()
    cfg["mac_voice_token"] = os.environ.get("MAC_VOICE_TOKEN", "").strip()
    cfg["crcmz_token"] = os.environ.get("CRCMZ_MCP_TOKEN", "").strip()
    return cfg


def now_ms():
    return time.time() * 1000.0


class Usage:
    """Provider-reported token counts per day, plus an estimate of their cost."""

    def __init__(self, path, prices):
        self.path, self.prices = path, prices
        self.data = {}
        try:
            self.data = json.loads(path.read_text())
        except (OSError, ValueError):
            pass

    def today(self):
        day = datetime.date.today().isoformat()
        if self.data.get("day") != day:
            self.data = {"day": day, "turns": 0, "audio_in_s": 0.0,
                         "tokens": {"in_audio": 0, "in_text": 0, "out_audio": 0,
                                    "out_text": 0, "total": 0}}
        # automated test turns are kept apart and never count toward the limits
        self.data.setdefault("test_tokens", {"in_audio": 0, "in_text": 0, "out_audio": 0,
                                             "out_text": 0, "total": 0})
        self.data.setdefault("test_turns", 0)
        return self.data

    def add_turn(self, audio_in_s, test=False):
        d = self.today()
        if test:
            d["test_turns"] += 1
        else:
            d["turns"] += 1
            d["audio_in_s"] = round(d["audio_in_s"] + audio_in_s, 2)
        self.save()

    def add_usage(self, um, test=False):
        d = self.today()["test_tokens" if test else "tokens"]

        def split(details, prefix):
            seen = 0
            for det in details or []:
                mod = str(getattr(det, "modality", "")).upper()
                n = det.token_count or 0
                seen += n
                d[f"{prefix}_audio" if "AUDIO" in mod else f"{prefix}_text"] += n
            return seen

        got_in = split(um.prompt_tokens_details, "in")
        got_out = split(um.response_tokens_details, "out")
        # anything not broken down by modality (e.g. thoughts) is billed as text
        d["in_text"] += max(0, (um.prompt_token_count or 0) - got_in)
        d["out_text"] += max(0, (um.response_token_count or 0) - got_out) + (um.thoughts_token_count or 0)
        d["total"] += um.total_token_count or 0
        self.save()

    def estimated_usd(self, bucket="tokens"):
        t = self.today()[bucket]
        return round(sum(t[k] * self.prices.get(k, 0) for k in
                         ("in_audio", "in_text", "out_audio", "out_text")) / 1e6, 4)

    def over_limit(self, limits, paid=True):
        d = self.today()
        if d["turns"] >= limits["max_turns_per_day"]:
            return "daily turn limit reached"
        if d["audio_in_s"] >= limits["max_audio_in_s_per_day"]:
            return "daily listening limit reached"
        if paid and self.estimated_usd() >= limits["max_estimated_usd_per_day"]:
            return "daily cost limit reached (estimate)"
        return None

    def save(self):
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data))
            tmp.replace(self.path)
        except OSError as e:
            log.warning("usage not saved: %s", e)


class Player:
    """One pacat per sink, fed in lock-step at real time with a small lead,
    so STOP only ever has a few tens of ms buffered and can kill them."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.procs = {}
        self.queue = collections.deque()
        self.queued_bytes = 0
        self.wake = asyncio.Event()
        self.t0 = None          # wall clock of the first byte of this run
        self.written = 0        # bytes written since t0
        self.last_audio = 0.0   # monotonic time the last AI audio will have finished
        self.on_first = None
        self.on_drained = None
        self.task = None

    def _spawn(self):
        for name, sink, ch in (("headset", self.cfg["headset_sink"], 2),
                               ("ps5", self.cfg["ps5_mic_sink"], 1)):
            p = self.procs.get(name)
            if p and p.poll() is None:
                continue
            self.procs[name] = subprocess.Popen(
                ["pacat", "--playback", "--raw", "--format=s16le", f"--rate={OUT_RATE}",
                 f"--channels={ch}", "--latency-msec=40", f"--device={sink}",
                 "--stream-name=psaudio-buddy", "--client-name=psaudio-buddy",
                 "--property=media.role=communication",
                 "--property=application.id=psaudio-buddy"],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                bufsize=0)

    def start(self):
        self._spawn()
        self.task = asyncio.create_task(self._run())

    def gain(self):
        return max(0, min(150, int(self.cfg["ai_volume"]))) / 100.0

    def push(self, pcm):
        limit = int(self.cfg["max_reply_s"] * OUT_RATE * 2)
        if self.queued_bytes + self.written + len(pcm) > limit:
            return False
        self.queue.append(pcm)
        self.queued_bytes += len(pcm)
        self.wake.set()
        return True

    def busy(self):
        return bool(self.queue) or self.t0 is not None

    def stop(self):
        """Drop everything now: queued audio and whatever pacat still holds."""
        self.queue.clear()
        self.queued_bytes = 0
        if self.t0 is not None:
            self.last_audio = time.monotonic()
        self.t0, self.written = None, 0
        for p in self.procs.values():
            if p.poll() is None:
                p.kill()
        self.procs = {}
        self._spawn()           # warm again for the next reply

    def close(self):
        if self.task:
            self.task.cancel()
        for p in self.procs.values():
            if p.poll() is None:
                p.kill()

    def _limit(self, pcm):
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
        x *= self.gain()
        c = float(self.cfg["limiter_ceiling"])
        knee = c * 0.7
        over = np.abs(x) > knee
        if over.any():          # soft knee: transparent below, tanh into the ceiling above
            a = np.abs(x[over]) - knee
            x[over] = np.sign(x[over]) * (knee + (c - knee) * np.tanh(a / (c - knee)))
        return (x * 32767.0).astype("<i2")

    async def _run(self):
        pending = b""
        while True:
            if not self.queue and len(pending) < OUT_FRAME:
                if self.t0 is not None and self.written:
                    # let what was written play out before calling it drained
                    left = self.t0 + self.written / (OUT_RATE * 2) - time.monotonic()
                    if left > 0:
                        await asyncio.sleep(left + 0.05)
                        continue
                    self.t0, self.written = None, 0
                    if pending:
                        pending = b""
                    if self.on_drained:
                        self.on_drained()
                self.wake.clear()
                await self.wake.wait()
                continue
            while self.queue and len(pending) < OUT_FRAME:
                b = self.queue.popleft()
                self.queued_bytes -= len(b)
                pending += b
            frame, pending = pending[:OUT_FRAME], pending[OUT_FRAME:]
            if len(frame) < OUT_FRAME and not self.queue:
                frame += b"\0" * (OUT_FRAME - len(frame))
            if self.t0 is None:
                self.t0, self.written = time.monotonic(), 0
                first = True
            else:
                first = False
            ahead = self.t0 + self.written / (OUT_RATE * 2) - time.monotonic()
            if ahead > PLAY_LEAD_S:
                await asyncio.sleep(ahead - PLAY_LEAD_S)
                if self.t0 is None:     # stopped while waiting
                    pending = b""
                    continue
            mono = self._limit(frame)
            self._spawn()
            for name, p in list(self.procs.items()):
                data = (np.repeat(mono, 2) if name == "headset" else mono).tobytes()
                try:
                    p.stdin.write(data)
                except (BrokenPipeError, OSError):
                    self.procs.pop(name, None)
            self.written += len(frame)
            self.last_audio = self.t0 + self.written / (OUT_RATE * 2)
            if first and self.on_first:
                self.on_first()


class Mic:
    """parec on one capture node. "mic" is the headset mic only — never a sink
    monitor, so game audio, party chat, soundboard and the AI's own voice
    can't get in. "squad" is the PS5's incoming mix, used only for the wake
    word and for a turn a squad member started with it."""

    def __init__(self, cfg, on_chunk, name="mic", source=None):
        self.cfg, self.on_chunk, self.name = cfg, on_chunk, name
        self.source = source or cfg["mic_source"]
        self.proc = None
        self.task = None
        self.taps = []            # e.g. the wake detector; fed every chunk
        self.preroll = collections.deque(maxlen=max(1, cfg["preroll_ms"] // 40))

    async def start(self):
        if self.proc and self.proc.returncode is None:
            return
        self.proc = await asyncio.create_subprocess_exec(
            "parec", "--raw", "--format=s16le", f"--rate={IN_RATE}", "--channels=1",
            "--latency-msec=20", f"--device={self.source}",
            f"--stream-name=psaudio-buddy-{self.name}", "--client-name=psaudio-buddy",
            "--property=application.id=psaudio-buddy",
            "--property=node.dont-reconnect=true",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        self.task = asyncio.create_task(self._read(self.proc))

    async def _read(self, proc):
        try:
            while True:
                chunk = await proc.stdout.readexactly(IN_CHUNK)
                for tap in self.taps:
                    tap(chunk)
                if not self.on_chunk(self.name, chunk):
                    self.preroll.append(chunk)   # memory only, ~200 ms, overwritten
        except (asyncio.IncompleteReadError, asyncio.CancelledError):
            pass

    def take_preroll(self):
        out = list(self.preroll)
        self.preroll.clear()
        return out

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.kill()
            await self.proc.wait()
        self.proc = None
        self.preroll.clear()


class WakeDetector:
    """Offline "Hey Hasaan" spotting with Vosk on one Mic, in its own thread.

    A small grammar (the wake phrases plus a few hundred everyday/gaming
    decoy words) keeps look-alikes like "hey, has anyone" from matching.
    After a trigger a full-vocabulary recogniser end-points the question
    (the grammar one would hear any word it doesn't know as silence). Events
    go back to the loop as on_event(name, kind, info). Audio is never stored."""

    TAIL = {"son", "sun", "san", "sean", "on", "hassan", "hasan", "sand", "song", "a", "the", "and"}

    DECOYS = ("he she they we you i a an the is it on in at to of and or but so has have had "
              "his her him hey hi okay ok yeah no yes go push left right back up down get got "
              "gun shot shotgun sniper roof ammo revive me please help someone anyone seen flag "
              "team son sun sam sean siri what time how where who that this was such hassle side "
              "careful now come here there one two three med kit reload loot drop map zone enemy "
              "behind cover nice good bad wow man bro dude let us lets").split()
    model = None
    model_lock = threading.Lock()

    def __init__(self, cfg, name, loop, on_event):
        import re
        self.cfg, self.name, self.loop, self.on_event = cfg, name, loop, on_event
        self.q = queue_mod.Queue(maxsize=50)     # ~2 s; full = drop, never pile up
        self.wake_re = re.compile(r"\b(" + "|".join(re.escape(p) for p in cfg["wake"]["phrases"]) + r")\b")
        self.mode = "idle"        # idle | capture
        self.paused = False       # session off
        self.gate = lambda: True  # False = don't trigger now (e.g. the AI is talking)
        self.streak = 0
        # quiet-after-words end-point, only on the clean headset mic (game audio is never quiet)
        self.quiet_end = name == "mic" and cfg["wake"].get("end_silence_ms", 0) > 0
        self.levels = collections.deque(maxlen=75)   # ~3 s of RMS before the wake word
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"wake-{name}")
        self.thread.start()

    def feed(self, chunk):
        if self.paused and self.mode == "idle":
            return
        try:
            self.q.put_nowait(chunk)
        except queue_mod.Full:
            pass

    def capture(self):
        """The turn this detector started: end-point it."""
        self.q.put(("mode", "capture"))

    def idle(self):
        self.q.put(("mode", "idle"))

    def _recognizer(self, vosk):
        phrases = list(self.cfg["wake"]["phrases"])
        r = vosk.KaldiRecognizer(WakeDetector.model, IN_RATE,
                                 json.dumps(phrases + sorted(set(self.DECOYS)) + ["[unk]"]))
        return r

    def _run(self):
        try:
            import vosk
            vosk.SetLogLevel(-1)
            with WakeDetector.model_lock:
                if WakeDetector.model is None:
                    WakeDetector.model = vosk.Model(os.path.expanduser(self.cfg["wake"]["model"]))
            r = self._recognizer(vosk)
            free = vosk.KaldiRecognizer(WakeDetector.model, IN_RATE)
        except Exception as e:
            self._emit("error", str(e)[:120])
            return
        self._emit("ready", None)
        after_words, spoke = 0, False
        while True:
            item = self.q.get()
            if isinstance(item, tuple):
                self.mode = item[1]
                r.Reset()
                free.Reset()
                self.streak, after_words, spoke = 0, 0, False
                continue
            x = np.frombuffer(item, dtype="<i2").astype(np.float32) / 32768.0
            rms = float(np.sqrt(np.mean(x * x)))
            if self.mode == "idle":
                self.levels.append(rms)
                final = r.AcceptWaveform(item)
                text = json.loads(r.Result() if final else r.PartialResult()).get(
                    "text" if final else "partial", "")
                if final or self.paused or not self.gate():
                    self.streak = 0
                    continue
                self.streak = self.streak + 1 if self.wake_re.search(text) else 0
                if self.streak >= 2:
                    self.mode = "capture"
                    self.streak, after_words, spoke = 0, 0, False
                    free.Reset()
                    floor = float(np.percentile(self.levels, 20)) if self.levels else 0.0
                    thr = max(0.004, floor * 3.0)
                    quiet = 0
                    self._emit("wake", text)
            else:
                # the question: words beyond the tail of "...saan"; its end-point submits it
                final = free.AcceptWaveform(item)
                text = json.loads(free.Result() if final else free.PartialResult()).get(
                    "text" if final else "partial", "")
                words = [w for w in self.wake_re.sub(" ", text).split()]
                n = len(words) if (len(words) > 1 or (words and words[0] not in self.TAIL)) else 0
                if os.environ.get("BUDDY_DEBUG") and text:
                    log.info("wake %s capture %s %r", self.name, "final" if final else "partial", text)
                if n and not spoke:
                    spoke = True
                    self._emit("speech", n)
                if self.quiet_end:
                    quiet = 0 if rms > thr else quiet + 40
                    if spoke and quiet >= self.cfg["wake"]["end_silence_ms"]:
                        self.mode = "idle"
                        r.Reset()
                        free.Reset()
                        self._emit("end", n or 1)
                        continue
                if not final:
                    continue
                after_words += n
                if after_words:
                    self.mode = "idle"
                    r.Reset()
                    free.Reset()
                    self._emit("end", after_words)

    def _emit(self, kind, info):
        self.loop.call_soon_threadsafe(self.on_event, self.name, kind, info)


class Buddy:
    def __init__(self, cfg):
        self.cfg = cfg
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self.usage = Usage(STATE_DIR / "usage.json", cfg["price_per_mtok"])
        self.profiles = local_backend.Profiles(CONFIG_PATH.parent / "profiles.json", cfg)
        self.local_history = collections.deque(maxlen=12)   # bounded local conversation
        self.mcp = None
        self.mcp_error = None
        self.live_tools = []          # crcmz tools offered to the current session
        self.tool_tasks = set()
        self.ack_task = None
        self.player = Player(cfg)
        self.player.on_first = self._first_played
        self.player.on_drained = self._drained
        self.mic = Mic(cfg, self._mic_chunk)
        self.squad = Mic(cfg, self._mic_chunk, name="squad", source=cfg["wake"]["squad_source"])
        self.session_on = False   # AI SESSION key: wake word active, connection on demand
        self.detectors = {}
        self.wake_handle = None
        self.last_wake = None
        self.held = None          # wake turns: audio kept on the Pi until speech follows
        self.state, self.error, self.seq = "off", None, 0
        self.changed = asyncio.Condition()
        self.armed = False
        self.session = None
        self.session_task = None
        self.session_ready = asyncio.Event()
        self.resume_handle = None
        self.listening = False
        self.listen_source = "mic"
        self.listen_started = 0.0
        self.listen_bytes = 0
        self.outbox = asyncio.Queue(maxsize=int(cfg["max_listen_s"] * 1000 / 40) + 16)
        self.sender_task = None
        self.turn = None          # timing + text of the current/last turn
        self.discard = False      # drop model audio until the server ends the cancelled turn
        self.gen_active = False   # the server is (or may be) still producing a reply
        self.activity_open = False
        self.last_use = time.monotonic()
        self.question, self.reply = "", ""
        self.history = collections.deque(maxlen=50)
        self.press_t = 0.0
        self.max_listen_handle = None
        self.mic_is_muted = None

    # -- state -------------------------------------------------------------
    async def set_state(self, state, error=None):
        if state == self.state and error == self.error:
            return
        self.state, self.error = state, error
        self.seq += 1
        log.info("state %s%s", state, f" ({error})" if error else "")
        async with self.changed:
            self.changed.notify_all()

    def bump(self):
        self.seq += 1
        asyncio.create_task(self._notify())

    async def _notify(self):
        async with self.changed:
            self.changed.notify_all()

    def status(self):
        u = self.usage.today()
        lat = [t for t in self.history if t.get("first_play_ms")]
        return {
            "seq": self.seq, "state": self.state, "error": self.error,
            "armed": self.session_on or self.armed, "session_on": self.session_on,
            "connected": self.session_ready.is_set(),
            "listening_source": self.listen_source if self.listening else None,
            "crcmz": {"enabled": self.mcp_conf()["enabled"], "token_set": bool(self.cfg["crcmz_token"]),
                      "tools_in_session": len(self.live_tools), "error": self.mcp_error},
            "wake": {"enabled": bool(self.cfg["wake"]["enabled"]),
                     "active": {k: (not d.paused) for k, d in self.detectors.items()},
                     "last": self.last_wake},
            "profile": {k: self.profile.get(k) for k in ("id", "label", "kind")},
            "model": self.model_name(), "voice": self.voice_name(),
            "ai_volume": self.cfg["ai_volume"], "mic_muted": self.mic_is_muted,
            "question": self.question, "reply": self.reply,
            "last_turn": self.history[-1] if self.history else None,
            "turns_measured": len(lat),
            "usage": {"day": u["day"], "turns": u["turns"], "audio_in_s": u["audio_in_s"],
                      "provider_tokens": u["tokens"],
                      "estimated_usd": self.usage.estimated_usd(),
                      "test_turns": u["test_turns"], "test_tokens": u["test_tokens"],
                      "test_estimated_usd": self.usage.estimated_usd("test_tokens"),
                      "note": "tokens are provider-reported; USD is an estimate, not billing"},
            "limits": self.cfg["limits"],
        }

    # -- session -------------------------------------------------------------
    @property
    def profile(self):
        return self.profiles.active

    def model_name(self):
        p = self.profile
        return p["model"] if p["kind"] == "gemini" else p["llm"]

    def voice_name(self):
        p = self.profile
        if p["kind"] == "gemini":
            return p["voice"]
        return "macOS Personal Voice" if p["tts_provider"] == "macos" else f'{p["tts_model"]}:{p["tts_voice"]}'

    async def use_profile(self, pid):
        """Switch model profile; an open connection is replaced, the wake word keeps going."""
        self.profiles.use(pid)
        self.resume_handle = None
        self.prepare_ack()
        log.info("profile -> %s (%s)", pid, self.model_name())
        if self.listening:
            await self._end_listen(submit=False)
        self.player.stop()
        if self.session_task and not self.session_task.done():
            self.session_task.cancel()
            try:
                await self.session_task
            except (asyncio.CancelledError, Exception):
                pass
            self.session_task = None
            self.session_ready.clear()
            self.armed = False
            await self.arm()
        elif self.state == "error":
            await self.set_state("ready" if self.session_on else "off")
        self.bump()

    def _stage(self, name):
        if self.turn is not None and name not in self.turn:
            self.turn[name] = now_ms()

    # -- crcmz MCP -----------------------------------------------------------------
    def mcp_conf(self):
        c = self.profiles.data.setdefault("mcp", {})
        c.setdefault("enabled", False)
        c.setdefault("url", "https://app.crcmz.me/mcp")
        c.setdefault("tools", list(mcp_client.DEFAULT_TOOLS))
        return c

    async def mcp_tools(self):
        """The crcmz read tools to offer this session ([] when off or unreachable)."""
        c = self.mcp_conf()
        if not c["enabled"] or not self.cfg["crcmz_token"]:
            return []
        try:
            if self.mcp is None or self.mcp.url != c["url"] or self.mcp.token != self.cfg["crcmz_token"]:
                if self.mcp:
                    await self.mcp.close()
                self.mcp = mcp_client.CrcmzMCP(c["url"], self.cfg["crcmz_token"])
            tools = mcp_client.selected(await asyncio.wait_for(self.mcp.tools(), 10), c["tools"])
            self.mcp_error = None
            return tools
        except Exception as e:
            self.mcp_error = f"{type(e).__name__}: {str(e)[:120]}"
            log.warning("crcmz MCP unavailable (continuing without it): %s", self.mcp_error)
            return []

    async def _run_tools(self, calls):
        """Gemini asked for crcmz data: fetch it (in parallel) and answer the calls."""
        session = self.session
        if self.turn is not None:
            self.turn.setdefault("tools", []).extend(fc.name for fc in calls)

        async def one(fc):
            text = await self.mcp.call(fc.name, dict(fc.args or {})) if self.mcp else "error: crcmz not connected"
            return types.FunctionResponse(id=fc.id, name=fc.name, response={"result": text})
        try:
            responses = await asyncio.gather(*(one(fc) for fc in calls))
            if session is not None and session is self.session:
                await session.send_tool_response(function_responses=list(responses))
        except Exception as e:
            log.warning("tool response failed: %s", e)

    # -- "Ahmm?" when the wake word is heard (instead of a beep) ---------------------
    def ack_conf(self):
        c = self.profiles.data.setdefault("ack", {})
        c.setdefault("enabled", True)
        c.setdefault("text", "Hmm?")
        return c

    def ack_path(self, p=None):
        import hashlib
        p, c = p or self.profile, self.ack_conf()
        key = json.dumps([c["text"], p["kind"], p.get("voice"), p.get("tts_provider"), p.get("tts_model"),
                          p.get("tts_voice"), p.get("ref_voice")], sort_keys=True)
        return STATE_DIR / "acks" / (hashlib.sha1(key.encode()).hexdigest()[:16] + ".wav")

    def prepare_ack(self, all_profiles=False):
        """Render the wake reply in the active voice (or every profile's), once, in the background."""
        if not self.ack_conf()["enabled"]:
            return
        todo = [p for p in (self.profiles.list() if all_profiles else [self.profile])
                if not self.ack_path(p).exists()]
        if not todo or (self.ack_task and not self.ack_task.done()):
            return

        async def run():
            for p in sorted(todo, key=lambda p: p["id"] != self.profile["id"]):   # active one first
                await self._render_ack(p)
        self.ack_task = asyncio.create_task(run())

    async def _render_ack(self, p):
        import base64
        import httpx
        text, out = self.ack_conf()["text"], self.ack_path(p)
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                if p["kind"] == "gemini":
                    r = await c.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{self.cfg['ack_tts_model']}:generateContent",
                        headers={"x-goog-api-key": self.cfg["api_key"]},
                        json={"contents": [{"parts": [{"text": text}]}], "generationConfig": {
                            "responseModalities": ["AUDIO"], "speechConfig": {"voiceConfig": {
                                "prebuiltVoiceConfig": {"voiceName": p["voice"]}}}}})
                    r.raise_for_status()
                    part = r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
                    raw, fmt = base64.b64decode(part["data"]), part.get("mimeType", "")
                    if "wav" not in fmt:          # raw PCM: s16le 24 kHz mono
                        raw = local_backend.pcm_to_wav(raw, 24000)
                elif p["tts_provider"] == "macos":
                    r = await c.post(p["mac_voice_url"].rstrip("/") + "/speak", json={"text": text},
                                     headers={"Authorization": f"Bearer {self.cfg['mac_voice_token']}"})
                    r.raise_for_status()
                    raw = r.content
                else:
                    body = {"model": p["tts_model"], "input": text, "voice": p.get("tts_voice"),
                            "response_format": "wav"}
                    ref = local_backend.voice_ref(p.get("ref_voice", "")) if "kokoro" not in p["tts_model"].lower() else None
                    if ref:
                        body["ref_audio"], body["ref_text"] = ref
                        body.pop("voice")
                    elif "kokoro" not in p["tts_model"].lower():
                        body.update(model=local_backend.LOCAL_DEFAULTS["tts_model"],
                                    voice=local_backend.LOCAL_DEFAULTS["tts_voice"])
                    r = await c.post(p["base_url"].rstrip("/") + "/audio/speech", json=body,
                                     headers={"Authorization": f"Bearer {self.cfg['omlx_key']}"})
                    r.raise_for_status()
                    raw = r.content
            trim = "silenceremove=start_periods=1:start_threshold=-42dB"
            out.parent.mkdir(parents=True, exist_ok=True)
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", "pipe:0", "-ac", "1",
                "-af", f"{trim},areverse,{trim},areverse,loudnorm=I=-18:TP=-2", "-ar", "24000",
                "-t", "2", str(out), stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(proc.communicate(raw), 30)
            if proc.returncode != 0:
                raise RuntimeError(err.decode(errors="ignore")[-120:])
            log.info("wake reply %r ready for %s", text, p["id"])
        except Exception as e:
            log.warning("wake reply not rendered (beep instead): %s", str(e)[:160])

    def personality(self):
        return (self.profiles.data.get("personality") or "").strip() or PERSONALITY

    def live_config(self):
        hint = mcp_client.TOOL_HINT if self.live_tools else ""
        c = dict(self.cfg, voice=self.profile["voice"], system_instruction=self.personality() + hint)
        extra = {}
        if self.live_tools:
            extra["tools"] = [types.Tool(function_declarations=mcp_client.gemini_declarations(self.live_tools))]
        return types.LiveConnectConfig(
            **extra,
            response_modalities=["AUDIO"],
            system_instruction=c["system_instruction"],
            max_output_tokens=int(c["max_output_tokens"]),
            speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=c["voice"]))),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            context_window_compression=types.ContextWindowCompressionConfig(
                trigger_tokens=int(c["context_trigger_tokens"]),
                sliding_window=types.SlidingWindow()),
            session_resumption=types.SessionResumptionConfig(handle=self.resume_handle),
        )

    async def arm(self):
        self.last_use = time.monotonic()
        if self.armed and self.session_task and not self.session_task.done():
            return
        gemini = self.profile["kind"] == "gemini"
        if gemini and genai is None:
            await self.set_state("error", "google-genai not installed")
            return
        if gemini and not self.cfg["api_key"]:
            await self.set_state("error", "no GEMINI_API_KEY in secrets.env")
            return
        if not gemini and not self.cfg["omlx_key"]:
            await self.set_state("error", "no OMLX_API_KEY in secrets.env")
            return
        self.armed = True
        self.player.stop()
        await self.mic.start()
        self.session_task = asyncio.create_task(self._session_loop())

    async def session_start(self):
        """AI SESSION on: connect now, and listen locally for the wake word."""
        self.session_on = True
        self.last_use = time.monotonic()
        await self.arm()
        w = self.cfg["wake"]
        if w["enabled"]:
            loop = asyncio.get_running_loop()
            for name, mic in (("mic", self.mic), ("squad", self.squad)):
                if not w.get(name):
                    continue
                d = self.detectors.get(name)
                if d is None:
                    d = self.detectors[name] = WakeDetector(self.cfg, name, loop, self._wake_event)
                    if name == "squad":
                        d.gate = self._squad_may_wake
                d.paused = False
                if d.feed not in mic.taps:
                    mic.taps.append(d.feed)
                await mic.start()
        self.bump()

    async def disconnect(self, why):
        """Close only the Gemini connection (idle); the wake word keeps listening."""
        if self.listening or self.player.busy():
            return
        self.armed = False
        if self.session_task:
            self.session_task.cancel()
            try:
                await self.session_task
            except (asyncio.CancelledError, Exception):
                pass
        self.session_task = None
        self.session_ready.clear()
        log.info("connection closed: %s (wake word still on)", why)
        if self.state not in ("error",):
            await self.set_state("ready" if self.session_on else "off")

    async def disarm(self, why="disarmed"):
        self.session_on = False
        for name, d in self.detectors.items():
            mic = self.mic if name == "mic" else self.squad
            if d.feed in mic.taps:
                mic.taps.remove(d.feed)
            d.paused = True       # thread idles on an empty queue until the next session
        await self.squad.stop()
        self.armed = False
        await self._end_listen(submit=False)
        self.player.stop()
        await self.mic.stop()
        if self.session_task:
            self.session_task.cancel()
            try:
                await self.session_task
            except (asyncio.CancelledError, Exception):
                pass
        self.session_task = None
        self.session_ready.clear()
        self.resume_handle = None
        log.info("session closed: %s", why)
        if self.state != "error":
            await self.set_state("off")

    def _connect(self, client):
        p = self.profile
        if p["kind"] == "local":
            hint = mcp_client.TOOL_HINT if self.live_tools else ""
            return local_backend.LocalSession(dict(self.cfg, system_instruction=self.personality() + hint),
                                              p, self.local_history, self._stage,
                                              mcp=self.mcp if self.live_tools else None, tools=self.live_tools)
        return client.aio.live.connect(model=p["model"], config=self.live_config())

    async def _session_loop(self):
        client = genai.Client(api_key=self.cfg["api_key"]) if genai and self.cfg["api_key"] else None
        backoff = 1.0
        while self.armed:
            if self.state not in ("listening",):
                await self.set_state("connecting")
            try:
                self.live_tools = await self.mcp_tools()
                t = time.monotonic()
                async with self._connect(client) as session:
                    self.session = session
                    self.discard = self.gen_active = False
                    if self.activity_open and not self.listening:
                        self.activity_open = False
                    self.session_ready.set()
                    log.info("connected to %s (%s) in %.0f ms, %d crcmz tools", self.model_name(),
                             self.profile["id"], (time.monotonic() - t) * 1000, len(self.live_tools))
                    if hasattr(session, "warm"):
                        asyncio.create_task(session.warm())
                    self.prepare_ack()
                    backoff = 1.0
                    if self.state in ("connecting", "error"):
                        await self.set_state("ready")
                    self.sender_task = asyncio.create_task(self._sender(session))
                    try:
                        await self._receiver(session)
                    finally:
                        self.sender_task.cancel()
                        self.session_ready.clear()
                        self.session = None
            except asyncio.CancelledError:
                raise
            except Exception as e:
                msg, fatal = self._describe(e)
                log.warning("live session error: %s", msg)
                self.session_ready.clear()
                if self.listening:
                    await self._end_listen(submit=False)
                await self.set_state("error", msg)
                if fatal:
                    self.armed = False
                    await self.mic.stop()
                    return
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue
            # clean close (go_away / 15 min limit): reconnect with the resume handle
            log.info("session ended by server; reconnecting")

    def _describe(self, e):
        text = str(e)
        if self.profile["kind"] == "local":
            import httpx
            if isinstance(e, LookupError):
                return text[:160], True
            if isinstance(e, httpx.HTTPStatusError) and e.response.status_code in (401, 403):
                return "oMLX refused the API key", True
            if isinstance(e, (httpx.ConnectError, httpx.ConnectTimeout, OSError)):
                return "MacBook / oMLX unreachable", False
            return f"local: {text[:150]}", False
        code = getattr(e, "code", None) or getattr(e, "status_code", None)
        low = text.lower()
        if "api key" in low or "api_key_invalid" in low or code in (401, 403) or "1008" in low and "key" in low:
            return "invalid or unauthorised API key", True
        if "not found" in low or "not supported" in low and "model" in low or code == 404:
            return f"model {self.model_name()} unavailable", True
        if code == 429 or "quota" in low or "resource_exhausted" in low or "rate" in low and "limit" in low:
            return "rate limited / quota exhausted", False
        if isinstance(e, (OSError, asyncio.TimeoutError)) or "connect" in low or "dns" in low:
            return "offline: cannot reach Gemini", False
        return text[:160], False

    async def _sender(self, session):
        while True:
            kind, payload = await self.outbox.get()
            try:
                if kind == "start":
                    await session.send_realtime_input(activity_start=types.ActivityStart())
                elif kind == "audio":
                    await session.send_realtime_input(
                        audio=types.Blob(data=payload, mime_type=f"audio/pcm;rate={IN_RATE}"))
                elif kind == "end":
                    await session.send_realtime_input(activity_end=types.ActivityEnd())
                    if self.turn is not None:
                        self.turn["end_sent_ms"] = now_ms()
            except Exception as e:
                log.warning("send failed: %s", e)
                raise

    async def _receiver(self, session):
        # receive() ends at each turn_complete; the session itself stays open
        while True:
            got = False
            async for msg in session.receive():
                got = True
                await self._handle(msg)
            if not got:          # socket closed (go_away / limit): reconnect
                return

    async def _handle(self, msg):
        if getattr(msg, "tool_call", None) and msg.tool_call.function_calls:
            task = asyncio.create_task(self._run_tools(msg.tool_call.function_calls))
            self.tool_tasks.add(task)
            task.add_done_callback(self.tool_tasks.discard)
        if msg.session_resumption_update and msg.session_resumption_update.new_handle:
            self.resume_handle = msg.session_resumption_update.new_handle
        if msg.go_away:
            log.info("go_away, %s left", msg.go_away.time_left)
        if msg.usage_metadata and self.profile["kind"] == "gemini":
            self.usage.add_usage(msg.usage_metadata, test=bool(self.turn and self.turn.get("test")))
        sc = msg.server_content
        if not sc:
            return
        if sc.input_transcription and sc.input_transcription.text:
            self.question += sc.input_transcription.text
            self.bump()
        if sc.model_turn and sc.model_turn.parts:
            for part in sc.model_turn.parts:
                if part.inline_data and part.inline_data.data and not self.discard:
                    self._audio_in(part.inline_data.data)
        if sc.output_transcription and sc.output_transcription.text and not self.discard:
            self.reply += sc.output_transcription.text
            self.bump()
        if os.environ.get("BUDDY_DEBUG"):
            log.info("rx %s", {k: bool(getattr(sc, k)) for k in (
                "model_turn", "turn_complete", "interrupted", "generation_complete",
                "input_transcription", "output_transcription")})
        if sc.interrupted:
            self.gen_active = False
            if self.discard:
                self.discard = False
            elif not self.listening:
                self.player.stop()
        if sc.turn_complete:
            self.gen_active = False
            if self.discard:
                self.discard = False
                return
            if self.turn is not None and not self.turn.get("logged"):
                self.turn["complete_ms"] = now_ms()
                self.turn["question"] = self.question[-300:]
                self.turn["reply"] = self.reply[-500:]
                self._log_turn()
            if self.state == "speaking" and not self.player.busy():
                await self.set_state("ready" if self.armed or self.session_on else "off")
            if self.state == "thinking":   # finished without any audio
                await self.set_state("ready" if self.armed or self.session_on else "off")

    def _audio_in(self, data):
        t = self.turn
        if t is not None and "first_chunk_ms" not in t:
            t["first_chunk_ms"] = now_ms()
        if self.listening:          # a new question already started; this is stale
            return
        if not self.player.push(data):
            log.info("reply hit max_reply_s; cut")
            self.interrupt()

    def _first_played(self):
        if self.turn is not None and "first_play_ms" not in self.turn:
            self.turn["first_play_ms"] = now_ms()
        asyncio.create_task(self.set_state("speaking"))

    def _drained(self):
        if self.gen_active:
            return                # a gap in the stream, not the end of the reply
        if self.state == "speaking":
            asyncio.create_task(self.set_state("ready" if self.armed or self.session_on else "off"))

    def _log_turn(self):
        t = dict(self.turn)
        base = t.get("release_ms") or t.get("end_ms")
        for k in ("end_sent_ms", "first_chunk_ms", "first_play_ms"):
            if base and t.get(k):
                t[k.replace("_ms", "_after_release_ms")] = round(t[k] - base, 1)
        self.history.append({k: v for k, v in t.items() if k not in ("question", "reply")})
        try:
            with open(STATE_DIR / "latency.jsonl", "a") as f:   # timings only, no audio/text
                f.write(json.dumps(self.history[-1]) + "\n")
        except OSError:
            pass
        self.turn["logged"] = True

    # -- listening -----------------------------------------------------------
    def interrupt(self):
        """Cancel a reply still being generated: an activity_start interrupts
        it server-side. The activity stays open for the next question, so no
        empty turn is ever sent (which the model would answer)."""
        if self.gen_active and self.session_ready.is_set():
            self.discard = True
            if not self.activity_open:
                self._queue(("start", None))
                self.activity_open = True

    def _queue(self, item):
        try:
            self.outbox.put_nowait(item)
        except asyncio.QueueFull:
            pass

    def _mic_chunk(self, name, chunk):
        if not self.listening or self.listen_source != name:
            return False
        if self.listen_bytes >= self.cfg["max_listen_s"] * IN_RATE * 2:
            return True
        self.listen_bytes += len(chunk)
        if self.held is not None:
            self.held.append(chunk)
        else:
            self._queue(("audio", chunk))
        return True

    def _release_held(self):
        """Speech followed the wake word: open the activity and send what was held."""
        if self.held is None:
            return
        held, self.held = self.held, None
        if not self.activity_open:
            self._queue(("start", None))
            self.activity_open = True
        for c in held:
            self._queue(("audio", c))

    async def start_listen(self, client_ms=None, source="mic", wake=False):
        self.last_use = time.monotonic()
        if self.listening:
            return
        why = self.usage.over_limit(self.cfg["limits"], paid=self.profile["kind"] == "gemini")
        if why:
            await self.set_state("error", why)
            return
        if source == "mic" and await self._mic_muted():
            await self.set_state("error", "mic muted")
            return
        # a new question always stops the previous reply
        self.player.stop()
        self.interrupt()
        if not self.armed or not self.session_task or self.session_task.done():
            await self.arm()
            if not self.armed:
                return
        self.question, self.reply = "", ""
        self.turn = {"press_ms": client_ms or now_ms(), "press_rx_ms": now_ms(),
                     "model": self.model_name(), "profile": self.profile["id"],
                     "connected_at_press": self.session_ready.is_set()}
        self.listening, self.listen_bytes = True, 0
        self.listen_source = source
        self.listen_started = time.monotonic()
        wake = source in ("mic", "squad") and wake
        self.held = [] if wake else None
        if not wake and not self.activity_open:
            self._queue(("start", None))
            self.activity_open = True
        if source in ("mic", "squad"):
            for c in (self.mic if source == "mic" else self.squad).take_preroll():
                if self.held is not None:
                    self.held.append(c)
                else:
                    self._queue(("audio", c))
                self.listen_bytes += len(c)
        limit = self.cfg["wake"]["squad_max_listen_s"] if source == "squad" else self.cfg["max_listen_s"]
        self.max_listen_handle = asyncio.get_running_loop().call_later(
            limit, lambda: asyncio.create_task(self.end_listen()))
        await self.set_state("listening")

    async def end_listen(self, client_ms=None):
        await self._end_listen(submit=True, client_ms=client_ms)

    async def _end_listen(self, submit, client_ms=None):
        if not self.listening:
            return
        self.listening = False
        if self.max_listen_handle:
            self.max_listen_handle.cancel()
        if self.wake_handle:
            self.wake_handle.cancel()
        d = self.detectors.get(self.listen_source)
        if d is not None and d.mode == "capture":
            d.idle()
        self.last_use = time.monotonic()
        if self.held is not None:
            if submit:
                self._release_held()          # e.g. the key was pressed to send it
            else:
                self.held = None              # nothing ever left the Pi
                self.listen_bytes = 0
        if self.turn is not None:
            self.turn["release_ms"] = client_ms or now_ms()
            self.turn["release_rx_ms"] = now_ms()
            self.turn["listen_s"] = round(self.listen_bytes / (IN_RATE * 2), 2)
        if submit and self.listen_bytes:
            self._queue(("end", None))
            self.activity_open = False
            self.gen_active = True
            self.usage.add_turn(self.listen_bytes / (IN_RATE * 2),
                                test=bool(self.turn and self.turn.get("test")))
            await self.set_state("thinking")
            if not self.session_ready.is_set():
                asyncio.create_task(self._connect_watchdog())
        elif self.listen_bytes and self.activity_open:
            # cancelled mid-question: close the activity and drop its answer
            self._queue(("end", None))
            self.activity_open = False
            self.gen_active = True
            self.discard = True
        if not (submit and self.listen_bytes) and self.state == "listening":
            await self.set_state("ready" if self.armed or self.session_on else "off")

    async def _connect_watchdog(self):
        try:
            await asyncio.wait_for(self.session_ready.wait(), 10)
        except asyncio.TimeoutError:
            if self.state == "thinking":
                await self.set_state("error", self.error or "could not connect")

    async def press(self, client_ms=None):
        self.press_t = time.monotonic()
        if self.listening:
            await self.end_listen(client_ms)     # second tap submits
        else:
            await self.start_listen(client_ms)

    async def release(self, client_ms=None):
        if self.listening and time.monotonic() - self.press_t >= self.cfg["hold_threshold_s"] \
                and self.listen_started >= self.press_t - 0.01:
            await self.end_listen(client_ms)     # held: release submits

    async def stop(self):
        self.last_use = time.monotonic()
        await self._end_listen(submit=False)
        self.player.stop()
        self.interrupt()
        if self.state in ("listening", "thinking", "speaking"):
            await self.set_state("ready" if self.armed or self.session_on else "off")
        elif self.state == "error":
            await self.set_state("ready" if self.armed or self.session_on else "off")

    def _wake_event(self, name, kind, info):
        asyncio.create_task(self._on_wake(name, kind, info))

    async def _on_wake(self, name, kind, info):
        w = self.cfg["wake"]
        if kind == "error":
            log.warning("wake word (%s) unavailable: %s", name, info)
            await self.set_state("error", f"wake word off: {info}")
            return
        if kind == "ready":
            log.info("wake word listening on %s", name)
            return
        d = self.detectors.get(name)
        if kind == "wake":
            if not self.session_on or d is None or (name == "squad" and not self._squad_may_wake()):
                d and d.idle()
                return
            if self.listening:
                d.idle()
                return
            log.info("wake word (%s): %r", name, info)
            self.last_wake = {"source": name, "at_ms": now_ms()}
            await self.start_listen(source=name, wake=True)
            if not self.listening:
                d.idle()
                return
            if w["chime"]:
                self._chime(to_squad=(name == "squad"))
            if self.wake_handle:
                self.wake_handle.cancel()
            self.wake_handle = asyncio.get_running_loop().call_later(
                w["no_speech_timeout_s"], lambda: asyncio.create_task(self._wake_timeout(name)))
        elif kind == "speech":
            if self.listening and self.listen_source == name and self.turn is not None:
                self.turn["heard_speech"] = True
                self._release_held()
                if self.wake_handle:
                    self.wake_handle.cancel()
        elif kind == "end":
            if self.listening and self.listen_source == name:
                if self.wake_handle:
                    self.wake_handle.cancel()
                await self.end_listen()

    async def _wake_timeout(self, name):
        if self.listening and self.listen_source == name and self.turn is not None \
                and not self.turn.get("heard_speech"):
            log.info("wake word (%s) but no question; cancelled", name)
            await self._end_listen(submit=False)

    def _squad_may_wake(self):
        """Squad wake only when nobody is asking and the AI has been quiet a moment,
        so its own voice coming back through a friend's speakers can't trigger it."""
        return (self.session_on and not self.listening and not self.player.busy()
                and time.monotonic() > self.player.last_audio + self.cfg["wake"]["squad_holdoff_s"])

    def _chime(self, to_squad):
        """Say the wake reply ("Ahmm?") in the active voice; the beep only if it isn't ready."""
        ack = self.ack_path()
        if self.ack_conf()["enabled"] and ack.exists():
            vol = int(max(0, min(150, int(self.cfg["ai_volume"]))) * 65536 / 100)
            sinks = [self.cfg["headset_sink"]] + ([self.cfg["ps5_mic_sink"]] if to_squad else [])
            for sink in sinks:
                subprocess.Popen(["paplay", f"--device={sink}", f"--volume={vol}", str(ack)],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        self.prepare_ack()
        path = STATE_DIR / "chime.wav"
        if not path.exists():
            t = np.arange(int(0.09 * 22050)) / 22050
            tone = np.concatenate([np.sin(2 * np.pi * 880 * t), np.sin(2 * np.pi * 1320 * t)])
            tone *= np.minimum(1, np.minimum(np.arange(len(tone)), len(tone) - np.arange(len(tone))) / 200)
            pcm = (tone * 0.25 * 32767).astype("<i2").tobytes()
            import wave
            with wave.open(str(path), "wb") as f:
                f.setnchannels(1); f.setsampwidth(2); f.setframerate(22050); f.writeframes(pcm)
        sinks = [self.cfg["headset_sink"]] + ([self.cfg["ps5_mic_sink"]] if to_squad else [])
        for sink in sinks:
            subprocess.Popen(["paplay", f"--device={sink}", str(path)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    async def _mic_muted(self):
        try:
            p = await asyncio.create_subprocess_exec(
                "pactl", "get-source-mute", self.cfg["mic_source"],
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(p.communicate(), 1.0)
            muted = b"yes" in out
            self.mic_is_muted = muted
            return muted
        except Exception:
            return False

    async def feed_file(self, path):
        """Test hook: stream a 16 kHz mono s16le file at real time as if spoken."""
        data = pathlib.Path(path).read_bytes()
        if data[:4] == b"RIFF":
            data = data[44:]
        await self.start_listen(source="file")
        if not self.listening:
            return None
        self.turn["test"] = True
        t0 = time.monotonic()
        for i in range(0, len(data), IN_CHUNK):
            if not self.listening:
                break
            self._queue(("audio", data[i:i + IN_CHUNK]))
            self.listen_bytes += len(data[i:i + IN_CHUNK])
            sleep = t0 + (i + IN_CHUNK) / (IN_RATE * 2) - time.monotonic()
            if sleep > 0:
                await asyncio.sleep(sleep)
        await self.end_listen()
        return len(data) / (IN_RATE * 2)

    async def mic_watch(self):
        while True:
            muted = await self._mic_muted()
            if muted != self.mic_is_muted:
                self.mic_is_muted = muted
                self.bump()
            await asyncio.sleep(2)

    async def idle_watch(self):
        while True:
            await asyncio.sleep(15)
            idle = time.monotonic() - self.last_use
            if self.listening or self.player.busy():
                continue
            if self.session_on and idle > self.cfg["wake"]["session_off_after_s"]:
                await self.disarm("AI SESSION unused")
            elif self.armed and idle > self.cfg["idle_timeout_s"]:
                if self.session_on:
                    await self.disconnect("idle timeout")
                else:
                    await self.disarm("idle timeout")


# -- HTTP ----------------------------------------------------------------------
def make_app(b: Buddy):
    cfg = b.cfg
    allow = set(cfg["allow"])

    @web.middleware
    async def guard(request, handler):
        if request.path == "/health":
            return await handler(request)
        peer = request.remote or ""
        if peer.startswith("::ffff:"):
            peer = peer[7:]
        if peer not in allow:
            return web.json_response({"ok": False, "error": "client not allowed"}, status=403)
        if not cfg["token"] or request.headers.get("Authorization") != f"Bearer {cfg['token']}":
            return web.json_response({"ok": False, "error": "unauthorised"}, status=401)
        return await handler(request)

    def client_ms(request):
        try:
            return float(request.query["t"])
        except (KeyError, ValueError):
            return None

    async def ok(_=None):
        return web.json_response({"ok": True, **b.status()})

    async def press(r):
        await b.press(client_ms(r)); return await ok()

    async def release(r):
        await b.release(client_ms(r)); return await ok()

    async def talk_start(r):
        await b.start_listen(client_ms(r)); return await ok()

    async def talk_end(r):
        await b.end_listen(client_ms(r)); return await ok()

    async def stop(r):
        await b.stop(); return await ok()

    async def arm(r):
        await b.session_start(); return await ok()

    async def disarm(r):
        await b.disarm(); return await ok()

    async def toggle(r):
        await (b.disarm() if b.session_on else b.session_start()); return await ok()

    async def volume(r):
        q = r.query
        v = int(cfg["ai_volume"])
        if "set" in q:
            v = int(q["set"])
        elif "up" in q:
            v += int(q["up"] or 5)
        elif "down" in q:
            v -= int(q["down"] or 5)
        cfg["ai_volume"] = max(0, min(150, v))
        b.bump()
        return await ok()

    async def status(r):
        since = int(r.query.get("since", -1))
        wait = min(55.0, float(r.query.get("wait", 0)))
        if since >= 0 and since == b.seq and wait > 0:
            async with b.changed:
                try:
                    await asyncio.wait_for(b.changed.wait_for(lambda: b.seq != since), wait)
                except asyncio.TimeoutError:
                    pass
        return await ok()

    async def history(r):
        return web.json_response({"ok": True, "turns": list(b.history)})

    async def test_feed(r):
        body = await r.json()
        p = pathlib.Path(body["file"]).resolve()
        if not str(p).startswith(str(STATE_DIR / "test")):
            return web.json_response({"ok": False, "error": "test files live in state/test"}, status=400)
        secs = await b.feed_file(p)
        return web.json_response({"ok": True, "fed_s": secs, **b.status()})

    async def profiles_get(r):
        return web.json_response({"ok": True, "active": b.profile["id"], "profiles": b.profiles.list()})

    async def profile_put(r):
        try:
            prof = b.profiles.put(r.match_info["pid"], await r.json())
        except (ValueError, KeyError) as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        local_backend._catalog_cache.clear()
        if prof["id"] == b.profile["id"]:
            await b.use_profile(prof["id"])          # reconnect with the edited settings
        return web.json_response({"ok": True, "profile": prof})

    async def profile_delete(r):
        pid = r.match_info["pid"]
        was_active = pid == b.profile["id"]
        try:
            b.profiles.delete(pid)
        except KeyError:
            return web.json_response({"ok": False, "error": "no such profile"}, status=404)
        except ValueError as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        if was_active:
            await b.use_profile(b.profile["id"])
        return web.json_response({"ok": True})

    async def profile_use(r):
        pid = r.match_info["pid"]
        if pid == "next":
            pid = b.profiles.next_id()
        if not b.profiles.get(pid):
            return web.json_response({"ok": False, "error": "no such profile"}, status=404)
        await b.use_profile(pid)
        return await ok()


    async def voices_get(r):
        return web.json_response({"ok": True, "voices": local_backend.list_voices()})

    async def _transcribe(wav_bytes):
        import httpx
        prof = b.profile if b.profile["kind"] == "local" else {**local_backend.LOCAL_DEFAULTS}
        async with httpx.AsyncClient(timeout=30) as c:
            res = await c.post(prof["base_url"].rstrip("/") + "/audio/transcriptions",
                               headers={"Authorization": f"Bearer {cfg['omlx_key']}"},
                               files={"file": ("v.wav", wav_bytes, "audio/wav")},
                               data={"model": prof.get("stt", "whisper-large-v3-turbo"), "response_format": "text",
                                     "language": prof.get("language") or "en"})
            res.raise_for_status()
            t = res.text.strip()
            return (json.loads(t).get("text", "") if t.startswith("{") else t).strip()

    async def _clean_voice(raw):
        """Any audio file -> 24 kHz mono 16-bit WAV: rumble cut, silence trimmed at
        both ends, loudness evened out, at most 30 s. The voice copy imitates
        whatever it is given, so a clean, even sample matters more than length."""
        trim = "silenceremove=start_periods=1:start_threshold=-45dB:start_silence=0.15"
        import tempfile
        # a real file, not a pipe: m4a from Voice Memos keeps its index at the end
        with tempfile.NamedTemporaryFile(dir=STATE_DIR, suffix=".upload") as tmp:
            os.chmod(tmp.name, 0o600)
            tmp.write(raw)
            tmp.flush()
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", tmp.name, "-vn", "-ac", "1",
                "-af", f"highpass=f=70,{trim},areverse,{trim},areverse,loudnorm=I=-19:TP=-2:LRA=11",
                "-ar", "24000", "-sample_fmt", "s16", "-t", "30", "-f", "wav", "pipe:1",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            out, err = await asyncio.wait_for(proc.communicate(), 60)
        if proc.returncode != 0 or len(out) < 1000:
            raise ValueError("couldn't read that audio file: " + err.decode(errors="ignore")[-120:].strip())
        # ffmpeg streams an unknown length into the header; fix the sizes so wave/oMLX can read it
        i = out.find(b"data")
        data = out[i + 8:]
        out = out[:i + 4] + len(data).to_bytes(4, "little") + data
        out = out[:4] + (len(out) - 8).to_bytes(4, "little") + out[8:]
        return out

    async def _save_voice(name, wav_bytes, text):
        import io as _io, wave as _wave
        wav_bytes = await _clean_voice(wav_bytes)
        with _wave.open(_io.BytesIO(wav_bytes)) as w:
            secs = w.getnframes() / w.getframerate()
        if not 5 <= secs <= 30:
            raise ValueError(f"after trimming silence it's {secs:.1f} s of speech; give it 10-30 s")
        if not text:
            text = await _transcribe(wav_bytes)
        local_backend.VOICES_DIR.mkdir(parents=True, exist_ok=True)
        os.chmod(local_backend.VOICES_DIR, 0o700)
        (local_backend.VOICES_DIR / f"{name}.wav").write_bytes(wav_bytes)
        (local_backend.VOICES_DIR / f"{name}.txt").write_text(text)
        local_backend._catalog_cache.clear()
        return {"name": name, "seconds": round(secs, 1), "text": text}

    async def voice_record(r):
        """Record your voice from the headset mic (memory only until saved here)."""
        import io as _io, wave as _wave
        name = r.match_info["name"]
        secs = max(8, min(30, int(r.query.get("seconds", 20))))
        if await b._mic_muted():
            return web.json_response({"ok": False, "error": "mic is muted - unmute it (key 4) first"}, status=409)
        proc = await asyncio.create_subprocess_exec(
            "parec", "--raw", "--format=s16le", "--rate=24000", "--channels=1",
            f"--device={cfg['mic_source']}", "--stream-name=psaudio-buddy-voice-record",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            pcm = await asyncio.wait_for(proc.stdout.readexactly(24000 * 2 * secs), secs + 5)
        except (asyncio.IncompleteReadError, asyncio.TimeoutError) as e:
            pcm = getattr(e, "partial", b"")
        finally:
            if proc.returncode is None:
                proc.kill()
        x = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        peak = float(np.max(np.abs(x))) if len(x) else 0.0
        if peak < 0.02:
            return web.json_response({"ok": False, "error": "heard almost nothing - is the headset on and the mic live?",
                                      "peak": round(peak, 3)}, status=422)
        buf = _io.BytesIO()
        with _wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000); w.writeframes(pcm)
        try:
            v = await _save_voice(name, buf.getvalue(), "")
        except Exception as e:
            return web.json_response({"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}, status=400)
        return web.json_response({"ok": True, "voice": v, "peak": round(peak, 3)})

    async def voice_put(r):
        name = r.match_info["name"]
        data = await r.read()
        if not 1000 < len(data) <= 35_000_000:
            return web.json_response({"ok": False, "error": "send an audio file (wav, aiff, m4a, mp3...) under 35 MB"}, status=400)
        try:
            v = await _save_voice(name, data, r.query.get("text", "").strip())
        except Exception as e:
            return web.json_response({"ok": False, "error": f"{type(e).__name__}: {str(e)[:160]}"}, status=400)
        return web.json_response({"ok": True, "voice": v})

    async def voice_text(r):
        """Fix the transcript if Whisper misheard a word (the copy needs it exact)."""
        name = r.match_info["name"]
        t = local_backend.VOICES_DIR / f"{name}.txt"
        if not t.with_suffix(".wav").exists():
            return web.json_response({"ok": False, "error": "no such voice"}, status=404)
        text = ((await r.json()).get("text") or "").strip()
        if not text:
            return web.json_response({"ok": False, "error": "empty transcript"}, status=400)
        t.write_text(text[:1000])
        local_backend._catalog_cache.clear()
        return web.json_response({"ok": True})

    async def voice_delete(r):
        name = r.match_info["name"]
        for ext in (".wav", ".txt"):
            (local_backend.VOICES_DIR / f"{name}{ext}").unlink(missing_ok=True)
        local_backend._catalog_cache.clear()
        return web.json_response({"ok": True})

    def _set_secret(key, value):
        path = CONFIG_PATH.parent / "secrets.env"
        lines = [l for l in path.read_text().splitlines() if not l.startswith(key + "=")] if path.exists() else []
        if value:
            lines.append(f"{key}={value}")
        tmp = path.with_suffix(".new")
        tmp.write_text("\n".join(lines) + "\n")
        os.chmod(tmp, 0o600)
        tmp.replace(path)

    async def ack_get(r):
        c = b.ack_conf()
        return web.json_response({"ok": True, "enabled": c["enabled"], "text": c["text"],
                                  "ready": b.ack_path().exists(), "profile": b.profile["id"]})

    async def ack_put(r):
        body = await r.json()
        c = b.ack_conf()
        if "enabled" in body:
            c["enabled"] = bool(body["enabled"])
        if "text" in body:
            t = str(body["text"]).strip()[:40]
            if not t:
                return web.json_response({"ok": False, "error": "say something, e.g. Ahmm?"}, status=400)
            c["text"] = t
        b.profiles.save()
        b.prepare_ack(all_profiles=True)
        if b.ack_task:
            try:
                await asyncio.wait_for(asyncio.shield(b.ack_task), 20)
            except Exception:
                pass
        return await ack_get(r)

    async def mcp_get(r):
        c = b.mcp_conf()
        out = {"ok": True, "enabled": c["enabled"], "url": c["url"], "selected": c["tools"],
               "defaults": mcp_client.DEFAULT_TOOLS, "token_set": bool(cfg["crcmz_token"]),
               "tools_in_session": len(b.live_tools), "tools": [], "error": b.mcp_error}
        if cfg["crcmz_token"]:
            try:
                if b.mcp is None or b.mcp.token != cfg["crcmz_token"] or b.mcp.url != c["url"]:
                    b.mcp = mcp_client.CrcmzMCP(c["url"], cfg["crcmz_token"])
                tools = await asyncio.wait_for(b.mcp.tools(force="refresh" in r.query), 12)
                out["tools"] = [{"name": t["name"], "description": (t.get("description") or "")[:220]}
                                for t in tools]
                out["error"] = None
            except Exception as e:
                out["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        return web.json_response(out)

    async def mcp_put(r):
        body = await r.json()
        c = b.mcp_conf()
        if "token" in body:
            tok = (body.get("token") or "").strip()
            if tok and not re.fullmatch(r"(crcmz_|mcpa-)[A-Za-z0-9_\-]{16,}", tok):
                return web.json_response({"ok": False, "error": "that doesn't look like a crcmz token (crcmz_...)"}, status=400)
            _set_secret("CRCMZ_MCP_TOKEN", tok)
            cfg["crcmz_token"] = tok
            if b.mcp:
                await b.mcp.close()
            b.mcp = None
        if "enabled" in body:
            c["enabled"] = bool(body["enabled"])
        if "url" in body and str(body["url"]).startswith("https://"):
            c["url"] = str(body["url"])
        if isinstance(body.get("tools"), list):
            c["tools"] = [str(n) for n in body["tools"] if not mcp_client.is_write(str(n))][:40]
        b.profiles.save()
        if b.session_task and not b.session_task.done():
            await b.use_profile(b.profile["id"])          # new tool set takes effect on reconnect
        return await mcp_get(r)

    async def personality_get(r):
        return web.json_response({"ok": True, "text": b.personality(), "default": PERSONALITY,
                                  "custom": bool((b.profiles.data.get("personality") or "").strip())})

    async def personality_put(r):
        text = ((await r.json()).get("text") or "").strip()
        if len(text) > 4000:
            return web.json_response({"ok": False, "error": "keep it under 4000 characters"}, status=400)
        b.profiles.data["personality"] = "" if text == PERSONALITY else text   # empty = default
        b.profiles.save()
        b.local_history.clear()                      # old replies would drag the old tone along
        if b.session_task and not b.session_task.done():
            await b.use_profile(b.profile["id"])     # Gemini takes it at connect: reconnect
        return await personality_get(r)

    async def catalog_get(r):
        return web.json_response({"ok": True, **await local_backend.catalog(b.cfg, b.profiles)})

    async def health(r):
        return web.json_response({"ok": True, "state": b.state})

    app = web.Application(middlewares=[guard], client_max_size=40 * 1024 * 1024)
    app.add_routes([
        web.post("/press", press), web.post("/release", release),
        web.post("/talk/start", talk_start), web.post("/talk/end", talk_end),
        web.post("/stop", stop), web.post("/arm", arm), web.post("/disarm", disarm),
        web.post("/toggle", toggle), web.post("/volume", volume),
        web.get("/status", status), web.get("/history", history),
        web.post("/test/feed", test_feed), web.get("/health", health),
        web.get("/profiles", profiles_get), web.put("/profiles/{pid}", profile_put),
        web.delete("/profiles/{pid}", profile_delete), web.post("/profiles/{pid}/use", profile_use),
        web.get("/catalog", catalog_get), web.get("/voices", voices_get),
        web.get("/personality", personality_get), web.put("/personality", personality_put),
        web.get("/mcp", mcp_get), web.put("/mcp", mcp_put),
        web.get("/ack", ack_get), web.put("/ack", ack_put),
        web.post("/voices/{name:[a-z0-9][a-z0-9_-]{0,31}}/record", voice_record),
        web.put("/voices/{name:[a-z0-9][a-z0-9_-]{0,31}}", voice_put),
        web.delete("/voices/{name:[a-z0-9][a-z0-9_-]{0,31}}", voice_delete),
        web.post("/voices/{name:[a-z0-9][a-z0-9_-]{0,31}}/text", voice_text),
    ])
    return app


async def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stderr)
    cfg = load_config()
    if not cfg["token"]:
        log.error("BUDDY_TOKEN missing from secrets.env; control API would be open — refusing")
        sys.exit(2)
    b = Buddy(cfg)
    b.player.start()
    if not cfg["api_key"]:
        await b.set_state("error", "no GEMINI_API_KEY in secrets.env")
    asyncio.create_task(b.idle_watch())
    asyncio.create_task(b.mic_watch())
    runner = web.AppRunner(make_app(b), access_log=None)
    await runner.setup()
    await web.TCPSite(runner, cfg["listen"], int(cfg["port"])).start()
    log.info("psaudio-buddy on %s:%s profile=%s model=%s voice=%s", cfg["listen"], cfg["port"],
             b.profile["id"], b.model_name(), b.voice_name())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(s, stop.set)
    await stop.wait()
    await b.disarm("shutdown")
    b.player.close()
    await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
