#!/usr/bin/env python3
"""Generate the built-in 8-bit style soundboard clips (original, no samples).

    python3 generate.py [output_dir]      # default: ./sounds
"""
import wave, math, struct, os, array, sys

SR = 44100
SOUNDS = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "sounds")
os.makedirs(SOUNDS, exist_ok=True)

def sine(freq, dur, vol=0.5, sr=SR):
    frames = int(sr * dur)
    return [int(vol * 32767 * math.sin(2 * math.pi * freq * i / sr)) for i in range(frames)]

def envelope(samples, attack=0.01, decay=0.1, sustain=0.6, release=0.2):
    n = len(samples)
    a, d = int(SR * attack), int(SR * decay)
    r = int(SR * release)
    s = n - a - d - r
    if s < 0: s = 0
    env = (
        [i / a for i in range(a)] +
        [1.0 - (1 - sustain) * (i / d) for i in range(d)] +
        [sustain] * s +
        [sustain * (1 - i / r) for i in range(r)]
    )
    return [int(s * e) for s, e in zip(samples, env)]

def write_wav(path, samples, channels=1):
    data = array.array('h', samples)
    with wave.open(path, 'w') as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(data.tobytes())

def concat(*parts):
    out = []
    for p in parts:
        out.extend(p)
    return out

def coin():
    """Mario-style coin: two quick ascending notes."""
    s1 = envelope(sine(987, 0.08), attack=0.005, decay=0.05, sustain=0.3, release=0.02)
    s2 = envelope(sine(1319, 0.12), attack=0.005, decay=0.08, sustain=0.2, release=0.03)
    return concat(s1, s2)

def oneup():
    """Mario 1-UP: ascending 6-note jingle."""
    notes = [523, 659, 784, 1047, 784, 1047]
    durs  = [0.08, 0.08, 0.08, 0.08, 0.08, 0.20]
    parts = []
    for freq, dur in zip(notes, durs):
        parts.append(envelope(sine(freq, dur), attack=0.005, decay=dur*0.4, sustain=0.5, release=0.03))
    return concat(*parts)

def victory():
    """Short triumphant fanfare."""
    notes = [523, 659, 784, 1047, 1319, 1047, 1319]
    durs  = [0.08, 0.08, 0.08, 0.08, 0.12, 0.08, 0.30]
    parts = []
    for freq, dur in zip(notes, durs):
        parts.append(envelope(sine(freq, dur), attack=0.005, decay=dur*0.3, sustain=0.6, release=0.05))
    return concat(*parts)

def gameover():
    """Descending sad notes."""
    notes = [523, 466, 415, 370, 330, 294]
    durs  = [0.15, 0.15, 0.15, 0.15, 0.15, 0.40]
    parts = []
    for freq, dur in zip(notes, durs):
        parts.append(envelope(sine(freq, dur), attack=0.01, decay=dur*0.3, sustain=0.5, release=0.08))
    return concat(*parts)

def alert():
    """Metal Gear '!' style: rapid sharp beeps."""
    beep = envelope(sine(1400, 0.07, vol=0.7), attack=0.002, decay=0.02, sustain=0.6, release=0.02)
    gap  = [0] * int(SR * 0.04)
    return concat(beep, gap, beep, gap, beep)

def powerup():
    """Ascending frequency sweep (power-up feeling)."""
    samples = []
    total = 0.5
    frames = int(SR * total)
    for i in range(frames):
        t = i / SR
        pct = i / frames
        freq = 220 * (2 ** (3 * pct))  # 220Hz to ~1760Hz
        vol = 0.5 * min(1.0, (1 - pct) * 3)
        samples.append(int(vol * 32767 * math.sin(2 * math.pi * freq * t)))
    return samples

def headshot():
    """Quick impact + high ring."""
    impact = []
    for i in range(int(SR * 0.03)):
        t = i / SR
        v = 0.8 * math.exp(-t * 60)
        impact.append(int(v * 32767 * (math.sin(2*math.pi*150*t) + 0.5*math.sin(2*math.pi*300*t))))
    ring = envelope(sine(1760, 0.3, vol=0.4), attack=0.002, decay=0.05, sustain=0.3, release=0.15)
    return concat(impact, ring)

def achievement():
    """Achievement unlocked: rising arpeggio + sustain chord."""
    notes = [523, 659, 784, 1047]
    parts = []
    for i, freq in enumerate(notes):
        parts.append(envelope(sine(freq, 0.12), attack=0.005, decay=0.05, sustain=0.6, release=0.04))
        if i < len(notes) - 1:
            parts.append([0] * int(SR * 0.02))
    # Final chord: all four together
    chord_len = int(SR * 0.5)
    chord = []
    for j in range(chord_len):
        t = j / SR
        v = 0.6 * math.exp(-j / (SR * 0.8))
        s = sum(math.sin(2 * math.pi * f * t) for f in notes) / len(notes)
        chord.append(int(v * 32767 * s))
    return concat(*parts, chord)

def combo():
    """Quick ascending combo sound: 4 quick taps."""
    notes = [440, 550, 660, 880]
    parts = []
    for freq in notes:
        parts.append(envelope(sine(freq, 0.06), attack=0.003, decay=0.03, sustain=0.4, release=0.01))
        parts.append([0] * int(SR * 0.01))
    return concat(*parts)

def countdown():
    """3-2-1 GO beeps."""
    low  = envelope(sine(440, 0.12), attack=0.005, decay=0.05, sustain=0.5, release=0.03)
    high = envelope(sine(880, 0.20), attack=0.005, decay=0.08, sustain=0.6, release=0.05)
    gap  = [0] * int(SR * 0.3)
    return concat(low, gap, low, gap, low, gap, high)

SOUND_FUNCS = {
    "coin.wav":        coin,
    "oneup.wav":       oneup,
    "victory.wav":     victory,
    "gameover.wav":    gameover,
    "alert.wav":       alert,
    "powerup.wav":     powerup,
    "headshot.wav":    headshot,
    "achievement.wav": achievement,
    "combo.wav":       combo,
    "countdown.wav":   countdown,
}

for name, fn in SOUND_FUNCS.items():
    path = os.path.join(SOUNDS, name)
    write_wav(path, fn())
    print(f"Generated: {name}")

print("Done.")
