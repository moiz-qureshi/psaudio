# psaudio-buddy — Gemini voice buddy ("Hasaan") for the PS Audio Bridge

Talk to an AI from the deck or by saying **"Hey Hasaan"**. The spoken reply
plays in the A50 X headset **and** goes out on the PS5 microphone, so the squad
hears it. Two kinds of model, switchable live (Deck Manager → **AI** page, or the
*AI model: next / set* deck actions):

- **Gemini Live** (`gemini-3.8-live`): native streaming audio in and out, no
  STT → LLM → TTS chain. Fastest (~0.75 s to first words). Google voices only.
- **Local, on the MacBook through oMLX**: Whisper → any chat model you pick
  (e.g. the uncensored Qwen) streamed → Kokoro, a voice cloned from your own
  recording (Qwen3-TTS), or your macOS Personal Voice (HasaanVoice app),
  spoken phrase by phrase while the model is still writing (~1.4–1.6 s). Free,
  private, but the Mac has to be awake.

The service only *adds* streams to PipeWire: one capture on the headset mic,
one on the PS5 incoming mix (wake word only), and one playback stream per
sink. It never edits the bridge's loopback, mic-effects or gadget config. If
it stops, crashes or loses the internet, game audio, your mic and the
soundboard keep working.

## Using it (soundboard page, bottom row)

| Key | What it does |
|---|---|
| **AI BUDDY** (5) | Hold to talk, release to send. A quick tap starts listening and a second tap sends. The icon shows OFF / LINKING / READY / LISTENING / THINKING / SPEAKING / ERROR. |
| **AI STOP** (6) | Stops the voice right away, drops queued audio and cancels the reply on Gemini's side. |
| **AI SESSION** (7) | On: connects to Gemini and turns on the wake word. Off: closes both. It switches itself off after 2 h unused; the Gemini connection alone closes after 15 min idle and reconnects on the next question. |
| **AI VOL − / +** (8, 9) | AI voice level, ±10 % (shown on the strip). |
| Key 4 | Headset mic to the PS5 on/off, using the Pi's real mute state. |

**Wake word** (only while AI SESSION is on):
- You say "Hey Hasaan …" into the headset: you hear a chime, it listens, and it
  sends about 0.6 s after you stop talking.
- A friend in party chat says "Hey Hasaan …": everyone hears the chime, and
  their question (mixed with whatever game audio is playing) goes to Gemini.
  The strip shows "listening to the squad".
- The squad wake word is ignored while the AI speaks and for 1.5 s afterwards,
  so its own voice coming back through a friend's speakers can't trigger it.
- "Hey Hasaan" with no question after it is dropped after 4 s, and **nothing is
  sent to Google**. Wake-turn audio stays on the Pi until real speech follows
  the wake word.
- A new question (key or wake word) always cuts the current reply.

Your mic muted to the PS5 also mutes it for the buddy: the key shows
"mic muted", and the wake word can't hear you.

## Personality

One personality for every model, edited on the AI page (**Personality** box;
*Reset to default* restores the built-in foul-mouthed, funny Hasaan). Stored as
`personality` in `profiles.json`; Gemini picks a change up on reconnect, which
saving does for you. No slurs or attacks on identity, by design: it roasts aim
and decisions.

## Model profiles (Deck Manager → AI)

Stored on the Pi in `~/.config/psaudio-buddy/profiles.json`, edited from the AI
page (`/api/aibuddy/*` in Deck Manager, a token-holding proxy) or the Pi API.
Each profile is `gemini` (model, voice) or `local` (oMLX URL, chat model,
speech-to-text model, voice source, temperature, max reply tokens, remembered
turns, language, extra instruction). Shipped profiles: *Gemini 3.8 Live*,
*Local · Qwen uncensored* (Kokoro voice), *Local · my cloned voice*
(Qwen3-TTS + your recording), *Local · macOS Personal Voice*. Switching
replaces the connection; the keys, wake word and STOP behave the same for all.

**Your voice, two ways**
- *Cloned:* AI page → **My voice** → put the headset on, unmute the mic,
  press **Record** and read the script (20 s) — or, better, record ~25 s on the
  MacBook (QuickTime / Voice Memos) and **Upload** it (m4a/wav/mp3). The Pi trims
  silence, evens the loudness and Whisper writes the transcript (editable).
  The recording stays in `~/.config/psaudio-buddy/voices/` on the Pi (mode
  700) and is sent to the Mac as the reference on each phrase. Delete it there
  any time.
- *macOS Personal Voice:* `integrations/macos-voice/` builds **HasaanVoice.app**
  on the Mac (`./build.sh`). It serves `/speak` on :8891 with a token (copied to
  the Pi as `MAC_VOICE_TOKEN`). macOS has to allow it (System Settings →
  Accessibility → Personal Voice → *Allow applications to request to use*, then
  Allow). Until macOS lists a Personal Voice to apps, it speaks with Samantha
  and the AI page says so.

**Local latency work** (all measured): Whisper gets a fixed language and plain
text output (−0.25 s). The Mac's GPU slows after ~2 s idle (+0.7 s per
request), so while someone is talking the Pi sends one chat request with the
real prompt (which also warms oMLX's prefix cache), then a one-letter Kokoro
request every second. The first spoken chunk breaks at the first clause.
Cloned-voice audio is streamed from oMLX and played as it arrives.

## Files

On the Pi (user `psaudio`, no root needed):

| Path | What |
|---|---|
| `~/.local/lib/psaudio-buddy/buddy.py`, `local_backend.py` | the service + local backend/profiles (`*.prev` = previous version) |
| `~/.config/psaudio-buddy/profiles.json`, `voices/` | model profiles; your voice recordings (local cloning) |
| `~/.local/lib/psaudio-buddy/.venv`, `requirements.lock` | pinned dependencies (`google-genai`, `aiohttp`, `numpy`, `vosk`) |
| `~/.local/lib/psaudio-buddy/models/vosk-model-small-en-us-0.15` | offline wake-word model (Apache-2.0) |
| `~/.config/psaudio-buddy/buddy.toml` | settings: model, voice, volume, timeouts, reply length, wake word, limits |
| `~/.config/psaudio-buddy/secrets.env` (600) | `GEMINI_API_KEY`, `BUDDY_TOKEN`, `OMLX_API_KEY`, `MAC_VOICE_TOKEN` |
| `~/.config/systemd/user/psaudio-buddy.service` | the user unit (`Restart=always`) |
| `~/.local/state/psaudio-buddy/usage.json`, `latency.jsonl` | daily usage counters; per-turn timings (no audio, no text) |

On opti3:

| Path | What |
|---|---|
| `~/.config/ragnaros/bin/aibuddy` | deck → Pi control client |
| `~/.config/ragnaros/bin/aibuddy-deck` + user unit `aibuddy-deck.service` | mirrors state onto the deck keys |
| `~/.config/ragnaros/secrets/aibuddy.env` (600) | Pi URL + bearer token |
| `~/.config/ragnaros/integrations/aibuddy.yaml` | Deck Manager actions |
| `~/.config/ragnaros/ragnarosd-plus.py` | gained `release:` key actions and a `keystate` verb |

Sources: `buddy/` (service) and `integrations/ragnaros-deck/` (deck side) in this repo.

## Install / update / key

```bash
rsync -a buddy/ psaudio-admin:src/psaudio-buddy/
ssh psaudio-admin 'cd ~/src/psaudio-buddy && ./install-buddy.sh'
ssh -t psaudio-admin /home/psaudio/.local/bin/buddy-key     # set or replace the Gemini key (hidden prompt)
```

`buddy-key` never puts the key in argv, shell history or logs. It checks the
key with Google and lists the Live models it can use.

## Control API (for CRCMZ later)

`http://192.168.5.151:8766`. Every call needs `Authorization: Bearer $BUDDY_TOKEN`,
and the client IP must be in `allow` (127.0.0.1 and opti3 by default).
`GET /health` is open but only returns the state.

`POST /press` `/release` `/talk/start` `/talk/end` `/stop` `/toggle` `/arm` `/disarm` `/volume?up=N|down=N|set=N`;
`GET /profiles`, `PUT|DELETE /profiles/{id}`, `POST /profiles/{id|next}/use`, `GET /catalog`
(models/voices available right now); `GET /voices`, `POST /voices/{name}/record?seconds=N`,
`PUT /voices/{name}` (WAV body), `DELETE /voices/{name}`;
`GET /status?since=SEQ&wait=25` (long-poll: state, latest question and reply
transcripts, mic mute, wake info, usage); `GET /history` (per-turn timings).

## Usage and cost

`/status.usage` holds the **token counts Google reports** per day (audio and
text, in and out). `estimated_usd` multiplies them by the paid-tier prices in
`buddy.toml` (from ai.google.dev/gemini-api/docs/pricing, checked 2026-10-25).
**That figure is an estimate, not your bill.** Actual charges are whatever
Google AI Studio / Cloud Billing shows, and free-tier usage costs nothing.
`[limits]` caps turns, listening seconds and estimated dollars per day; at a
cap the key shows an error until the next day.

## Measured latency (2026-10-25, software timestamps)

| Path | Median | Worst |
|---|---|---|
| Key release → turn end sent | 2.5 ms | 3.8 ms |
| Key release → first Gemini audio chunk | 750 ms | 948 ms |
| First chunk → handed to playback | ~1–3 ms | |
| Hand-off → audio in the PS5-mic sink mix | +50–120 ms | |
| Wake word: end of speech → turn sent | 554 ms | 576 ms |
| Wake word: end of speech → first audio | 1.24 s | 1.91 s |
| Local Qwen + Kokoro: release → first audio | 1.36–1.75 s | 2.07 s |
| Local Qwen + cloned voice: release → first audio | 1.55 s | 1.95 s |

These are Pi-side software numbers. The A50 X wireless link adds its own
delay, the PS5/PSN path to friends adds more, and audible latency was not
measured. Nearly all of the remaining time is Gemini's own time to first audio.

`testmode.sh on|off` on the Pi points the buddy at virtual inputs and a null
sink, so tests are silent for you and the squad.

## Disable / roll back

```bash
# switch it off (bridge unaffected)
ssh psaudio-admin 'systemctl --user disable --now psaudio-buddy'
# previous service version
ssh psaudio-admin 'cd ~/.local/lib/psaudio-buddy && cp buddy.py.prev buddy.py && systemctl --user restart psaudio-buddy'
# remove completely (the Pi bridge config was never changed)
ssh psaudio-admin 'systemctl --user disable --now psaudio-buddy; rm -rf ~/.local/lib/psaudio-buddy ~/.config/psaudio-buddy ~/.local/state/psaudio-buddy ~/.config/systemd/user/psaudio-buddy.service ~/.local/bin/buddy-key; systemctl --user daemon-reload'
# deck side: restore the backed-up files (path in ~/.aibuddy-backup-path)
B=$(cat ~/.aibuddy-backup-path); cp $B/ragnarosd-plus.py ~/.config/ragnaros/; cp $B/soundboard.yaml ~/.config/ragnaros/profiles/
systemctl --user disable --now aibuddy-deck; rm ~/.config/ragnaros/integrations/aibuddy.yaml; systemctl --user restart ragnarosd
```

To turn off only the wake word: in `buddy.toml` set `[wake] enabled = false`
(or just `squad = false` to stop friends triggering it), then
`systemctl --user restart psaudio-buddy`.
