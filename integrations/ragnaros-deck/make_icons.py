"""Render the soundboard page's key icons with deckui into ~/.config/ragnaros/assets/icons/.

    python3 make_icons.py            # writes the set, prints what it wrote
Keys with `state: <tag>` use icons/<tag>_<state>.png (see ragnarosd-plus keystate).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from deckui import key_icon  # noqa: E402

OUT = os.path.expanduser("~/.config/ragnaros/assets/icons")
ICONS = {
    # Hasaan push-to-talk key (state: aibuddy)
    "aibuddy":            ("HASAAN", "TALK", "bot", "cyan", True, None),
    "aibuddy_off":        ("HASAAN", "OFF", "bot", "slate", False, None),
    "aibuddy_connecting": ("HASAAN", "LINKING", "dots", "amber", True, "amber"),
    "aibuddy_ready":      ("HASAAN", "READY", "bot", "cyan", True, "cyan"),
    "aibuddy_listening":  ("HASAAN", "LISTENING", "mic", "red", True, "red"),
    "aibuddy_thinking":   ("HASAAN", "THINKING", "dots", "amber", True, "amber"),
    "aibuddy_speaking":   ("HASAAN", "SPEAKING", "bars", "green", True, "green"),
    "aibuddy_error":      ("HASAAN", "ERROR", "alert", "red", True, "red"),
    "aibuddy_offline":    ("HASAAN", "NO LINK", "unlink", "red", True, None),
    "aistop":             ("AI", "STOP", "stop", "red", True, None),
    # always-on session (state: aisession)
    "aisession":          ("SESSION", "TOGGLE", "power", "slate", False, None),
    "aisession_on":       ("SESSION", "ON", "power", "green", True, "green"),
    "aisession_off":      ("SESSION", "OFF", "power", "slate", False, None),
    # roast (state: airoast)
    "airoast":            ("HOLD", "ROAST", "flame", "orange", True, None),
    "airoast_idle":       ("HOLD", "ROAST", "flame", "orange", True, None),
    "airoast_listening":  ("ROAST", "SAY NAME", "mic", "red", True, "red"),
    "airoast_thinking":   ("ROAST", "COOKING", "flame", "amber", True, "amber"),
    "airoast_speaking":   ("ROAST", "ROASTING", "bars", "orange", True, "orange"),
    # squad mode: always listening + watching (state: aisquad)
    "aisquad":            ("SQUAD", "JOIN", "squad", "slate", False, None),
    "aisquad_off":        ("SQUAD", "JOIN", "squad", "slate", False, None),
    "aisquad_on":         ("SQUAD", "LISTENING", "squad", "violet", True, "violet"),
    "aisquad_watching":   ("SQUAD", "WATCHING", "eye", "violet", True, "green"),
    "aisquad_talking":    ("SQUAD", "TALKING", "bars", "green", True, "green"),
    # plain action keys
    "ui_studio":          ("LIGHT", "STUDIO", "bulb", "amber", True, None),
    "ui_hdmi":            ("TV INPUT", "HDMI", "hdmi", "violet", True, None),
}

if __name__ == "__main__":
    for name, spec in ICONS.items():
        key_icon(*spec).save(os.path.join(OUT, name + ".png"))
        print(name)
