"""Local voice backend for psaudio-buddy, plus the model profiles store.

A LocalSession speaks the same tiny interface buddy.py uses for a Gemini Live
session (send_realtime_input / receive), so the deck keys, wake word, STOP,
barge-in, routing and limits behave identically. Behind it, one turn is:

  headset audio --(Whisper on oMLX)--> text --(chat model on oMLX, streamed)-->
  sentences --(Kokoro on oMLX, or your macOS Personal Voice via HasaanVoice)-->
  24 kHz PCM --> the same player that feeds the headset + PS5 mic.

Each sentence is spoken as soon as it is complete while the model keeps
writing, so the first words come out before the reply is finished.
"""
import asyncio
import collections
import io
import json
import logging
import os
import pathlib
import re
import time
import wave
from types import SimpleNamespace as NS

import httpx
import numpy as np

log = logging.getLogger("buddy")

IN_RATE, OUT_RATE = 16000, 24000
SENTENCE_END = re.compile(r"[.!?…]+[\"')\]]*\s")
CLAUSE_END = re.compile(r"[,;:—–]\s")


def split_ready(text, first):
    """Return (speakable chunk, rest) as soon as a chunk can be voiced.
    The first chunk breaks at the first clause (>= 3 words) so speech starts
    early; later ones at sentence ends, or at a clause once they get long."""
    m = SENTENCE_END.search(text)
    if m and m.end() >= 8:
        return text[:m.end()].strip(), text[m.end():]
    limit = 18 if first else 140
    for m in CLAUSE_END.finditer(text):
        if m.end() >= limit and len(text[:m.end()].split()) >= 3:
            return text[:m.end()].strip(), text[m.end():]
    return None, text
THINK = re.compile(r"<think>.*?</think>\s*", re.S)
FILLERS = ["Hold up, checking.", "One sec, pulling it up.", "Hang on, let me look.", "Lemme check the receipts."]


def _json_args(text):
    try:
        v = json.loads(text or "{}")
        return v if isinstance(v, dict) else {}
    except ValueError:
        return {}


def openai_tools(tools):
    from mcp_client import openai_tools as conv
    return conv(tools)

GEMINI_VOICES = ["Puck", "Charon", "Kore", "Fenrir", "Aoede", "Leda", "Orus", "Zephyr",
                 "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
                 "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
                 "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
                 "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat"]

PROFILE_FIELDS = {
    "gemini": {"label": str, "model": str, "voice": str},
    "local": {"label": str, "base_url": str, "llm": str, "stt": str, "tts_provider": str,
              "tts_model": str, "tts_voice": str, "mac_voice_url": str, "temperature": float,
              "max_tokens": int, "history_turns": int, "extra_instruction": str, "language": str,
              "ref_voice": str},
}
LOCAL_DEFAULTS = {
    "base_url": "http://192.168.4.38:1234/v1", "stt": "whisper-large-v3-turbo",
    "tts_provider": "omlx", "tts_model": "Kokoro-82M-bf16", "tts_voice": "am_puck",
    "mac_voice_url": "http://192.168.4.38:8891", "temperature": 0.8, "max_tokens": 220,
    "history_turns": 4, "extra_instruction": "", "language": "en", "ref_voice": "",
}
VOICES_DIR = pathlib.Path(os.path.expanduser("~/.config/psaudio-buddy/voices"))


# -- cloned voices: a short recording + its transcript, kept only on the Pi ------
def list_voices():
    out = []
    for w in sorted(VOICES_DIR.glob("*.wav")) if VOICES_DIR.exists() else []:
        txt = w.with_suffix(".txt")
        try:
            with wave.open(str(w)) as f:
                secs = round(f.getnframes() / f.getframerate(), 1)
        except Exception:
            secs = None
        out.append({"name": w.stem, "seconds": secs,
                    "text": txt.read_text().strip() if txt.exists() else ""})
    return out


def voice_ref(name):
    """(base64 wav, transcript) for a cloned voice, or None."""
    import base64
    w = VOICES_DIR / f"{name}.wav"
    if not name or not w.exists():
        return None
    t = w.with_suffix(".txt")
    return base64.b64encode(w.read_bytes()).decode(), (t.read_text().strip() if t.exists() else "")


# -- profiles -------------------------------------------------------------------
class Profiles:
    """~/.config/psaudio-buddy/profiles.json: {"active": id, "profiles": [..]}.
    Edited through the API (Deck Manager's AI page); written atomically."""

    def __init__(self, path, cfg):
        self.path = path
        self.data = None
        try:
            self.data = json.loads(path.read_text())
        except (OSError, ValueError):
            pass
        if not self.data or not self.data.get("profiles"):
            self.data = {"active": "gemini", "profiles": [
                {"id": "gemini", "kind": "gemini", "label": "Gemini 3.8 Live",
                 "model": cfg["model"], "voice": cfg["voice"]},
                {"id": "local-uncensored", "kind": "local", "label": "Local · Qwen uncensored",
                 "llm": "Qwen3.6-35B-A3B-Uncensored-Heretic-MLX-8bit", **LOCAL_DEFAULTS},
                {"id": "local-myvoice", "kind": "local", "label": "Local · my voice",
                 "llm": "Qwen3.6-35B-A3B-Uncensored-Heretic-MLX-8bit",
                 **{**LOCAL_DEFAULTS, "tts_provider": "macos"}},
            ]}
            self.save()

    def list(self):
        return self.data["profiles"]

    def get(self, pid):
        return next((p for p in self.data["profiles"] if p["id"] == pid), None)

    @property
    def active(self):
        return self.get(self.data.get("active")) or self.data["profiles"][0]

    def use(self, pid):
        if not self.get(pid):
            raise KeyError(pid)
        self.data["active"] = pid
        self.save()

    def next_id(self):
        ids = [p["id"] for p in self.data["profiles"]]
        return ids[(ids.index(self.active["id"]) + 1) % len(ids)]

    def put(self, pid, body):
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,31}", pid or ""):
            raise ValueError("id: lowercase letters, digits, - and _ (max 32)")
        kind = body.get("kind") or (self.get(pid) or {}).get("kind")
        if kind not in PROFILE_FIELDS:
            raise ValueError("kind must be gemini or local")
        base = dict(self.get(pid) or ({**LOCAL_DEFAULTS} if kind == "local" else {}))
        for k, typ in PROFILE_FIELDS[kind].items():
            if k in body and body[k] is not None:
                try:
                    base[k] = typ(body[k])
                except (TypeError, ValueError):
                    raise ValueError(f"{k}: expected {typ.__name__}")
        if kind == "local":
            if base.get("tts_provider") not in ("omlx", "macos"):
                raise ValueError("tts_provider must be omlx or macos")
            if not str(base.get("base_url", "")).startswith(("http://", "https://")):
                raise ValueError("base_url must be http(s)://...")
            if not base.get("llm"):
                raise ValueError("llm (chat model) is required")
        elif not base.get("model"):
            raise ValueError("model is required")
        base.update(id=pid, kind=kind)
        base.setdefault("label", pid)
        existing = self.get(pid)
        if existing:
            self.data["profiles"][self.data["profiles"].index(existing)] = base
        else:
            self.data["profiles"].append(base)
        self.save()
        return base

    def delete(self, pid):
        p = self.get(pid)
        if not p:
            raise KeyError(pid)
        if len(self.data["profiles"]) == 1:
            raise ValueError("keep at least one profile")
        self.data["profiles"].remove(p)
        if self.data["active"] == pid:
            self.data["active"] = self.data["profiles"][0]["id"]
        self.save()

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2))
        tmp.replace(self.path)


# -- catalog: what can be picked -------------------------------------------------
_catalog_cache = {}


async def catalog(cfg, profiles):
    """Models/voices each kind can use, fetched live (cached 60 s)."""
    now = time.monotonic()
    if _catalog_cache.get("t", 0) > now - 60:
        return _catalog_cache["data"]
    out = {"gemini": {"models": [], "voices": GEMINI_VOICES, "error": None},
           "local": {}, "macos": {}, "cloned_voices": list_voices()}
    async with httpx.AsyncClient(timeout=6) as c:
        if cfg.get("api_key"):
            try:
                r = await c.get("https://generativelanguage.googleapis.com/v1beta/models",
                                params={"pageSize": 200}, headers={"x-goog-api-key": cfg["api_key"]})
                r.raise_for_status()
                out["gemini"]["models"] = sorted(
                    m["name"].split("/", 1)[1] for m in r.json().get("models", [])
                    if "bidiGenerateContent" in m.get("supportedGenerationMethods", [])
                    and ("live" in m["name"] or "native-audio" in m["name"])
                    and not any(x in m["name"] for x in ("transcribe", "translate")))
            except Exception as e:
                out["gemini"]["error"] = str(e)[:120]
        urls = {p.get("base_url") for p in profiles.list() if p["kind"] == "local"} or {LOCAL_DEFAULTS["base_url"]}
        for url in urls:
            entry = {"llm": [], "stt": [], "tts": [], "voices": {}, "error": None}
            try:
                h = {"Authorization": f"Bearer {cfg.get('omlx_key', '')}"}
                r = await c.get(url.rstrip("/") + "/models", headers=h)
                r.raise_for_status()
                for m in (x["id"] for x in r.json().get("data", [])):
                    low = m.lower()
                    if "whisper" in low or "transcribe" in low:
                        entry["stt"].append(m)
                    elif "tts" in low or "kokoro" in low:
                        entry["tts"].append(m)
                    elif not any(s in low for s in ("embed", "rerank", "flux", "-vl-", "vl-")):
                        entry["llm"].append(m)
                for m in entry["tts"]:
                    try:
                        v = await c.get(url.rstrip("/") + "/audio/voices", params={"model": m}, headers=h)
                        entry["voices"][m] = v.json().get("voices", [])
                    except Exception:
                        entry["voices"][m] = []
            except Exception as e:
                entry["error"] = f"{type(e).__name__}: {str(e)[:100]}"
            out["local"][url] = entry
        for url in {p.get("mac_voice_url") for p in profiles.list()
                    if p["kind"] == "local" and p.get("tts_provider") == "macos"}:
            try:
                out["macos"][url] = (await c.get(url.rstrip("/") + "/health")).json()
            except Exception as e:
                out["macos"][url] = {"ok": False, "error": f"{type(e).__name__}"}
    _catalog_cache.update(t=now, data=out)
    return out


# -- the session ---------------------------------------------------------------
def _msg(**sc):
    """A stand-in for google.genai's LiveServerMessage with just what buddy reads."""
    content = NS(model_turn=None, turn_complete=False, interrupted=False, generation_complete=False,
                 input_transcription=None, output_transcription=None, error=None)
    for k, v in sc.items():
        setattr(content, k, v)
    return NS(session_resumption_update=None, go_away=None, usage_metadata=None, server_content=content)


def tts_token_cap(text):
    """Upper bound on audio tokens for a phrase (Qwen3-TTS runs at 12 tokens per second
    of speech): ~3x the time the text should take, so it can never run on."""
    return max(48, min(600, 24 + int(len(text) * 1.6)))


def pcm_to_wav(pcm, rate):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate); w.writeframes(pcm)
    return buf.getvalue()


def _wav_to_pcm24k(data):
    with wave.open(io.BytesIO(data)) as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError(f"TTS returned {width * 8}-bit audio")
    x = np.frombuffer(raw, dtype="<i2")
    if ch > 1:
        x = x.reshape(-1, ch).mean(axis=1).astype("<i2")
    if rate != OUT_RATE:      # only when the voice isn't already 24 kHz (Kokoro and HasaanVoice are)
        t = np.arange(0, len(x) * OUT_RATE / rate) * rate / OUT_RATE
        x = np.interp(t, np.arange(len(x)), x.astype(np.float32)).astype("<i2")
    return x.tobytes()


class LocalSession:
    def __init__(self, cfg, profile, history, on_stage, mcp=None, tools=None, on_tool=None):
        self.cfg, self.p = cfg, profile
        self.on_tool = on_tool            # buddy plays a pre-rendered "let me look that up"
        self.turn_mode = {}               # set by the buddy at the end of each question (ROAST key)
        self.mcp = mcp                    # crcmz MCP client (read-only tools), or None
        self.tools = tools or []
        self.history = history            # deque of (user, assistant), bounded by the buddy
        self.on_stage = on_stage          # on_stage(name) -> records a timing on the turn
        self.audio = bytearray()
        self.out = asyncio.Queue()
        self.task = None
        self.keepwarm = None
        base = profile["base_url"].rstrip("/")
        self.base = base
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(20, connect=4),
            headers={"Authorization": f"Bearer {cfg.get('omlx_key', '')}"},
            limits=httpx.Limits(max_keepalive_connections=4))

    async def __aenter__(self):
        r = await self.client.get(self.base + "/models")   # fail fast: wrong URL / key / Mac asleep
        r.raise_for_status()
        ids = {m["id"] for m in r.json().get("data", [])}
        missing = [m for m in (self.p["llm"], self.p["stt"]) if m not in ids]
        if self.p["tts_provider"] == "omlx" and self.p["tts_model"] not in ids:
            missing.append(self.p["tts_model"])
        if missing:
            raise LookupError(f"not on oMLX: {', '.join(missing)}")
        if self.p["tts_provider"] == "macos":
            h = (await self.client.get(self.p["mac_voice_url"].rstrip("/") + "/health")).json()
            if not h.get("ok"):
                raise LookupError("HasaanVoice on the Mac is not answering")
        return self

    async def warm(self):
        """One tiny request per model so the first real question doesn't pay load time."""
        t = time.monotonic()
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(IN_RATE)
            w.writeframes(b"\0" * IN_RATE)
        jobs = [self.client.post(self.base + "/audio/transcriptions",
                                 files={"file": ("w.wav", buf.getvalue(), "audio/wav")},
                                 data={"model": self.p["stt"], "response_format": "text",
                                       "language": self.p.get("language") or "en"}),
                self.client.post(self.base + "/chat/completions", json={
                    "model": self.p["llm"], "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                    "chat_template_kwargs": {"enable_thinking": False}})]
        if self.p["tts_provider"] == "omlx":
            jobs.append(self.client.post(self.base + "/audio/speech", json={
                "model": LOCAL_DEFAULTS["tts_model"], "input": "ok", "voice": LOCAL_DEFAULTS["tts_voice"],
                "response_format": "wav"}))
        await asyncio.gather(*jobs, return_exceptions=True)
        log.info("local models warm in %.0f ms", (time.monotonic() - t) * 1000)

    async def __aexit__(self, *exc):
        for t in (self.task, self.keepwarm):
            if t:
                t.cancel()
        await self.client.aclose()

    def _messages(self, text):
        system = self.cfg["system_instruction"] + (
            " Reply in plain spoken English: no markdown, lists, emojis or stage directions.")
        if self.p.get("extra_instruction"):
            system += " " + self.p["extra_instruction"]
        msgs = [{"role": "system", "content": system}]
        for u, a in list(self.history)[-int(self.p.get("history_turns", 4)):]:
            msgs += [{"role": "user", "content": u}, {"role": "assistant", "content": a}]
        if text is not None:
            msgs.append({"role": "user", "content": text})
        return msgs

    async def _keep_warm(self):
        """While someone is talking. The Mac's GPU slows down after ~2 s idle
        (+0.7 s on the next request), so: one chat request at the start with
        the real system prompt + history (leaves that prefix in oMLX's cache),
        then a one-letter Kokoro request every second (~0.08 s of GPU, and the
        cheapest thing to collide with when the real request arrives)."""
        try:
            await asyncio.wait_for(self.client.post(self.base + "/chat/completions", json={
                "model": self.p["llm"], "max_tokens": 1, "messages": self._messages("..."),
                "chat_template_kwargs": {"enable_thinking": False}}), 3)
        except Exception:
            pass
        while True:
            await asyncio.sleep(1.0)
            try:
                # always Kokoro: a voice-copy model with no sample can run on and jam
                # oMLX's audio queue (that is what stuck the buddy on "thinking")
                await asyncio.wait_for(self.client.post(self.base + "/audio/speech", json={
                    "model": LOCAL_DEFAULTS["tts_model"], "input": "k",
                    "voice": LOCAL_DEFAULTS["tts_voice"], "response_format": "wav"}), 2)
            except Exception:
                pass

    # buddy -> session
    async def send_realtime_input(self, activity_start=None, audio=None, activity_end=None, **_):
        if activity_start is not None:
            if self.task and not self.task.done():
                self.task.cancel()              # a new question interrupts the reply
                await self.out.put(_msg(interrupted=True))
            self.audio = bytearray()
            if self.keepwarm is None or self.keepwarm.done():
                self.keepwarm = asyncio.create_task(self._keep_warm())
        elif audio is not None:
            if len(self.audio) < self.cfg["max_listen_s"] * IN_RATE * 2:
                self.audio += audio.data
        elif activity_end is not None:
            if self.keepwarm:
                self.keepwarm.cancel()
            pcm, self.audio = bytes(self.audio), bytearray()
            self.task = asyncio.create_task(self._turn(pcm))

    # session -> buddy (ends at each turn_complete, like the Gemini SDK)
    async def receive(self):
        while True:
            m = await self.out.get()
            yield m
            if m.server_content.turn_complete or m.server_content.interrupted:
                return

    async def _turn(self, pcm):
        try:
            await self._run(pcm)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            why = ("MacBook busy (oMLX not answering)" if isinstance(e, httpx.TimeoutException)
                   else "MacBook unreachable" if isinstance(e, httpx.ConnectError)
                   else f"local: {type(e).__name__} {str(e)[:80]}")
            log.warning("local turn failed: %s (%r)", why, e)
            await self.out.put(_msg(turn_complete=True, error=why))
            return
        await self.out.put(_msg(turn_complete=True))

    async def _run(self, pcm):
        if len(pcm) < IN_RATE * 2 * 0.3:
            return
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(IN_RATE); w.writeframes(pcm)
        # a fixed language + plain text skips detection and segment output (~0.25 s)
        form = {"model": self.p["stt"], "response_format": "text"}
        if self.p.get("language"):
            form["language"] = self.p["language"]
        t = time.monotonic()
        r = await self.client.post(self.base + "/audio/transcriptions",
                                   files={"file": ("q.wav", buf.getvalue(), "audio/wav")}, data=form)
        if os.environ.get("BUDDY_DEBUG"):
            log.info("local stt %.0f ms for %.1f s audio", (time.monotonic() - t) * 1000, len(pcm) / (IN_RATE * 2))
        r.raise_for_status()
        text = r.text.strip()
        if text.startswith("{"):
            try:
                text = (json.loads(text).get("text") or "").strip()
            except ValueError:
                pass
        self.on_stage("stt_done_ms")
        if not text or re.fullmatch(r"[\s.,!?]*(thank you|thanks for watching|you)?[\s.,!?]*", text, re.I):
            return                               # silence or Whisper's usual silence hallucinations
        await self.out.put(_msg(input_transcription=NS(text=text)))

        roast = self.turn_mode.get("mode") == "roast"
        if roast:
            # ROAST key: one savage roast of a random player, no lookups, no history
            msgs = [{"role": "system", "content": self.cfg["system_instruction"].split(" You can look things up")[0]
                     + " " + self.turn_mode.get("directive", "")},
                    {"role": "user", "content": text}]
        else:
            msgs = self._messages(text)

        sentences = asyncio.Queue()
        speaker = asyncio.create_task(self._speak(sentences))
        reply = ""
        try:
            for _round in range(1 if roast else 3):     # at most two rounds of crcmz lookups
                text_out, calls = await self._stream_llm(msgs, sentences, first_reply=not reply, roast=roast)
                reply = (reply + " " + text_out).strip() if text_out else reply
                if not calls or not self.mcp:
                    break
                if not text_out:                       # cover the lookup time with a filler line
                    if self.on_tool:
                        self.on_tool()                 # instant, pre-rendered in this voice
                    else:
                        await sentences.put(FILLERS[int(time.time()) % len(FILLERS)])
                self.on_stage("tool_call_ms")
                msgs.append({"role": "assistant", "content": text_out or None, "tool_calls": [
                    {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["args"]}}
                    for c in calls]})
                results = await asyncio.gather(*(self.mcp.call(c["name"], _json_args(c["args"])) for c in calls))
                for c, res in zip(calls, results):
                    msgs.append({"role": "tool", "tool_call_id": c["id"], "name": c["name"], "content": res})
            await sentences.put(None)
            await speaker
        finally:
            if not speaker.done():
                speaker.cancel()
        if reply:
            self.history.append((text, reply))

    async def _stream_llm(self, msgs, sentences, first_reply, roast=False):
        """One streamed completion: speak text as it comes; return (text, tool calls)."""
        body = {"model": self.p["llm"], "messages": msgs, "stream": True,
                "max_tokens": max(320, int(self.p.get("max_tokens", 220))) if roast else int(self.p.get("max_tokens", 220)),
                "temperature": max(1.0, float(self.p.get("temperature", 0.8))) if roast else float(self.p.get("temperature", 0.8)),
                "chat_template_kwargs": {"enable_thinking": False}}
        if self.tools and self.mcp and not roast:
            body["tools"] = openai_tools(self.tools)
        reply, pending, in_think, calls = "", "", False, {}
        async with self.client.stream("POST", self.base + "/chat/completions", json=body) as resp:
            resp.raise_for_status()
            first = True
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    d = json.loads(data)["choices"][0].get("delta", {})
                except (ValueError, KeyError, IndexError):
                    continue
                for tc in d.get("tool_calls") or []:          # streamed in pieces, by index
                    c = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                    c["id"] = tc.get("id") or c["id"]
                    fn = tc.get("function") or {}
                    c["name"] += fn.get("name") or ""
                    c["args"] += fn.get("arguments") or ""
                delta = d.get("content") or ""
                if not delta:
                    continue
                if first:
                    self.on_stage("llm_first_token_ms")
                    first = False
                pending += delta
                if "<think>" in pending:
                    in_think = True
                if in_think:
                    if "</think>" not in pending:
                        continue
                    pending = THINK.sub("", pending)
                    in_think = False
                while True:
                    chunk, pending = split_ready(pending, first=first_reply and not reply)
                    if not chunk:
                        break
                    reply += (" " if reply else "") + chunk
                    await sentences.put(chunk)
        tail = THINK.sub("", pending).strip()
        if tail:
            reply += (" " if reply else "") + tail
            await sentences.put(tail)
        out = [dict(c, id=c["id"] or f"call_{i}") for i, c in sorted(calls.items()) if c["name"]]
        return reply, out

    async def _speak(self, sentences):
        first = True
        while True:
            s = await sentences.get()
            if s is None:
                return
            s = re.sub(r"[*_#`~>]+", "", s)
            if self.p["tts_provider"] == "macos":
                url = self.p["mac_voice_url"].rstrip("/") + "/speak"
                r = await self.client.post(url, json={"text": s},
                                           headers={"Authorization": f"Bearer {self.cfg.get('mac_voice_token', '')}"})
                r.raise_for_status()
                pcm = _wav_to_pcm24k(r.content)
                if first:
                    self.on_stage("tts_first_ms")
                    first = False
                step = OUT_RATE * 2 // 10                   # hand over in 100 ms pieces
                for i in range(0, len(pcm), step):
                    await self.out.put(_msg(model_turn=NS(parts=[NS(inline_data=NS(data=pcm[i:i + step]))])))
            else:
                async for pcm in self._tts_stream(s):
                    if first:
                        self.on_stage("tts_first_ms")
                        first = False
                    await self.out.put(_msg(model_turn=NS(parts=[NS(inline_data=NS(data=pcm))])))
            await self.out.put(_msg(output_transcription=NS(text=(" " + s))))

    async def _tts_stream(self, text):
        """oMLX streams one WAV header then raw PCM as it is generated; pass the
        audio on as it arrives instead of waiting for the whole phrase."""
        body = {"model": self.p["tts_model"], "input": text, "voice": self.p.get("tts_voice") or None,
                "response_format": "wav", "stream": True, "max_tokens": tts_token_cap(text)}
        ref = voice_ref(self.p.get("ref_voice", "")) if "tts" in self.p["tts_model"].lower() and \
            "kokoro" not in self.p["tts_model"].lower() else None
        if ref:
            body["ref_audio"], body["ref_text"] = ref
            body.pop("voice")
        elif "base" in self.p["tts_model"].lower() and "kokoro" not in self.p["tts_model"].lower():
            # a voice-cloning model with no recording yet: speak with Kokoro instead of failing
            body.update(model=LOCAL_DEFAULTS["tts_model"], voice=LOCAL_DEFAULTS["tts_voice"])
        async with self.client.stream("POST", self.base + "/audio/speech", json=body) as r:
            r.raise_for_status()
            head, rate, odd = b"", None, b""
            async for chunk in r.aiter_bytes():
                if rate is None:              # parse the header once (fmt rate, start of data)
                    head += chunk
                    i = head.find(b"data")
                    if i < 0 or len(head) < i + 8:
                        continue
                    f = head.find(b"fmt ")
                    rate = int.from_bytes(head[f + 12:f + 16], "little") if f >= 0 else OUT_RATE
                    chunk, head = head[i + 8:], b""
                chunk = odd + chunk
                if len(chunk) % 2:
                    chunk, odd = chunk[:-1], chunk[-1:]
                else:
                    odd = b""
                if not chunk:
                    continue
                if rate != OUT_RATE:
                    x = np.frombuffer(chunk, dtype="<i2").astype(np.float32)
                    t = np.arange(0, len(x) * OUT_RATE / rate) * rate / OUT_RATE
                    chunk = np.interp(t, np.arange(len(x)), x).astype("<i2").tobytes()
                yield chunk
