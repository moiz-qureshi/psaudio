#!/usr/bin/env python3
"""End-to-end checks for psaudio-buddy, run on the Pi:

  ~/.local/lib/psaudio-buddy/.venv/bin/python test_buddy.py [trials]

Feeds the synthetic questions in ~/.local/state/psaudio-buddy/test/q*.pcm
through the real Live session and listens to the *sink monitors* - i.e. the
actual mix going to the A50 X and to the PS5 microphone - to see when the AI
voice appears there, that STOP silences it, and that a new question cuts the
old reply. Nothing is written to disk except the printed report.
"""
import json
import os
import pathlib
import statistics
import subprocess
import sys
import threading
import time
import urllib.request

import numpy as np

HOME = pathlib.Path.home()
TEST = HOME / ".local/state/psaudio-buddy/test"
URL = "http://127.0.0.1:8766"
TOKEN = next(l.split("=", 1)[1].strip() for l in open(HOME / ".config/psaudio-buddy/secrets.env")
             if l.startswith("BUDDY_TOKEN="))
SINKS = {"headset": "alsa_output.usb-Logitech_A50_X-00.analog-stereo.monitor",
         "ps5_mic": "alsa_output.platform-fe980000.usb.mono-fallback.monitor"}
RATE, BLOCK = 16000, 160          # 10 ms blocks


def api(path, method="GET", body=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method=method, headers={
        "Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


class Meter(threading.Thread):
    """RMS per 10 ms of one monitor, with wall-clock timestamps (memory only)."""

    def __init__(self, device=None, stream=None):
        super().__init__(daemon=True)
        src = [f"--monitor-stream={stream}"] if stream is not None else [f"--device={device}"]
        self.p = subprocess.Popen(["parec", "--raw", "--format=s16le", f"--rate={RATE}",
                                   "--channels=1", "--latency-msec=10", *src,
                                   "--stream-name=buddy-test-meter"],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.samples = []   # (t_ms, rms, peak)
        self.start()

    def run(self):
        while True:
            b = self.p.stdout.read(BLOCK * 2)
            if not b:
                return
            x = np.frombuffer(b, dtype="<i2").astype(np.float32) / 32768
            self.samples.append((time.time() * 1000, float(np.sqrt(np.mean(x * x))),
                                 float(np.max(np.abs(x)))))

    def first_above(self, t0, thr):
        return next((t for t, r, _ in self.samples if t >= t0 and r > thr), None)

    def last_above(self, t0, t1, thr):
        hits = [t for t, r, _ in self.samples if t0 <= t <= t1 and r > thr]
        return hits[-1] if hits else None

    def peak(self, t0, t1):
        vals = [p for t, _, p in self.samples if t0 <= t <= t1]
        return max(vals) if vals else 0.0

    def stop(self):
        self.p.kill()


def buddy_streams():
    """sink-input index of the buddy's stream on each sink."""
    out = subprocess.run(["pactl", "-f", "json", "list", "sink-inputs"], capture_output=True,
                         text=True).stdout
    sinks = {s["index"]: s["name"] for s in json.loads(subprocess.run(
        ["pactl", "-f", "json", "list", "sinks"], capture_output=True, text=True).stdout)}
    res = {}
    for si in json.loads(out):
        if si["properties"].get("application.id") == "psaudio-buddy":
            name = sinks.get(si["sink"], "")
            res["buddy_headset" if "A50_X" in name else "buddy_ps5_mic"] = si["index"]
    return res


def xruns():
    total = 0
    for f in pathlib.Path("/proc/asound").glob("card*/pcm*/sub0/status"):
        for line in f.read_text().splitlines():
            if line.strip().startswith("xrun"):
                total += int(line.split(":")[1])
    return total


def wait_state(states, timeout=30):
    t = time.time()
    while time.time() - t < timeout:
        st = api("/status")
        if st["state"] in states:
            return st
        time.sleep(0.02)
    return api("/status")


def feed(name, block=True):
    if block:
        return api("/test/feed", "POST", {"file": str(TEST / name)})
    th = threading.Thread(target=feed, args=(name,), daemon=True)
    th.start()
    return th


def main():
    trials = int(sys.argv[1]) if len(sys.argv) > 1 else 6
    qs = sorted(p.name for p in TEST.glob("q*.pcm"))
    meters = {k: Meter(v) for k, v in SINKS.items()}
    time.sleep(1.0)
    base = {k: np.percentile([r for _, r, _ in m.samples] or [0], 95) for k, m in meters.items()}
    thr = {k: max(0.004, b * 3) for k, b in base.items()}
    print(f"baseline rms p95: {base}  -> thresholds {thr}")

    api("/arm", "POST")
    st = wait_state({"ready"}, 15)
    assert st["state"] == "ready", st
    x0 = xruns()
    for k, idx in buddy_streams().items():
        meters[k] = Meter(stream=idx)
        thr[k] = 0.004
    time.sleep(0.5)
    rows = []
    for i in range(trials):
        q = qs[i % len(qs)]
        st = feed(q)
        fin = wait_state({"ready", "error"}, 40)
        time.sleep(0.3)
        t = fin["last_turn"] or {}
        rel = t.get("release_ms")
        row = {"q": q, "heard_q": fin["question"].strip()[:60], "reply": fin["reply"].strip()[:80],
               "end_sent": t.get("end_sent_after_release_ms"),
               "first_chunk": t.get("first_chunk_after_release_ms"),
               "first_play": t.get("first_play_after_release_ms")}
        for k, m in meters.items():
            hit = m.first_above(rel, thr[k]) if rel else None
            row[f"at_{k}_sink"] = round(hit - rel, 1) if hit else None
            row[f"peak_{k}"] = round(m.peak(rel, t.get("complete_ms", rel) + 30000), 3) if rel else None
        rows.append(row)
        print(json.dumps(row))
        time.sleep(0.5)

    print("\n== latency after release (ms), n=%d ==" % len(rows))
    for k in ("end_sent", "first_chunk", "first_play", "at_buddy_headset_sink",
              "at_buddy_ps5_mic_sink", "at_headset_sink", "at_ps5_mic_sink"):
        v = [r[k] for r in rows if r[k] is not None]
        if v:
            print(f"{k:18s} median {statistics.median(v):7.1f}   worst {max(v):7.1f}   n={len(v)}")
    for k in ("peak_buddy_headset", "peak_buddy_ps5_mic", "peak_ps5_mic"):
        v = [r[k] for r in rows if r[k] is not None]
        print(f"{k:18s} max {max(v):.3f}  (limiter ceiling 0.85 x sink volume)")

    print(f"ALSA xruns during trials: {xruns() - x0}")

    # STOP: let it speak, stop, measure how long the monitors keep sounding
    print("\n== STOP ==")
    for n in range(3):
        for k, idx in buddy_streams().items():
            if k in meters:
                meters[k].stop()
            meters[k] = Meter(stream=idx)
        time.sleep(0.3)
        feed(qs[1])
        wait_state({"speaking"}, 20)
        time.sleep(0.6)
        t_stop = time.time() * 1000
        api("/stop", "POST")
        t_ack = time.time() * 1000
        time.sleep(1.5)
        res = {"api_ms": round(t_ack - t_stop, 1)}
        for k, m in meters.items():
            last = m.last_above(t_stop - 200, t_stop + 1500, thr[k])
            res[f"{k}_sound_until_ms"] = round(last - t_stop, 1) if last else "silent"
        res["state_after"] = api("/status")["state"]
        res["buddy_streams_after"] = buddy_streams()
        time.sleep(2.0)       # any audio from the cancelled turn would show up here
        for k, m in meters.items():
            res[f"{k}_leak_later"] = m.last_above(t_stop + 300, time.time() * 1000, thr[k]) is not None
        print(json.dumps(res))
        time.sleep(1)

    # barge-in: a new question while the old reply plays
    print("\n== new question cuts old reply ==")
    feed(qs[1])
    wait_state({"speaking"}, 20)
    time.sleep(0.5)
    feed(qs[3])
    fin = wait_state({"ready"}, 40)
    print(json.dumps({"heard_q": fin["question"].strip()[:60], "reply": fin["reply"].strip()[:100]}))

    # what does the buddy's mic stream connect to?
    links = subprocess.run(["pw-link", "-l"], capture_output=True, text=True).stdout
    print("\n== buddy capture links ==")
    lines = links.splitlines()
    for j, l in enumerate(lines):
        if l.startswith("psaudio-buddy") and "input" in l:
            print(l, lines[j + 1] if j + 1 < len(lines) else "")
    for m in meters.values():
        m.stop()


if __name__ == "__main__":
    main()
