"""deckui - the deck's design kit: one look for key icons and the live strip.

"Dark glass HUD": near-black cards with a soft depth gradient, a thin glowing
accent edge, one big word per key (Space Grotesk Bold), chunky vector glyphs.
Everything is drawn at 3x and scaled down, so edges stay crisp on the small
LCDs. Legibility rules: one word per key, >= 18 px labels, high contrast.

    from deckui import key_icon, Strip
"""
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

SS = 3                                       # supersampling factor
FONT_DIRS = [os.path.expanduser("~/.local/share/fonts"), "/usr/share/fonts/truetype/lato",
             "/usr/share/fonts/truetype/dejavu"]

# palette
BG = (9, 11, 16)
CARD_TOP, CARD_BOTTOM = (24, 28, 40), (12, 14, 21)
TEXT, DIM, FAINT = (238, 241, 247), (136, 143, 163), (70, 76, 96)
ACCENT = {
    "cyan": (34, 211, 238), "red": (244, 63, 94), "green": (52, 211, 153), "slate": (100, 108, 130),
    "orange": (251, 146, 60), "amber": (251, 191, 36), "violet": (167, 139, 250), "blue": (96, 165, 250),
}

_fonts = {}


def font(size, weight=700):
    """Space Grotesk at a weight (300-700); Lato Black / DejaVu as fallbacks."""
    key = (size, weight)
    if key in _fonts:
        return _fonts[key]
    f = None
    sg = os.path.join(FONT_DIRS[0], "SpaceGrotesk-var.ttf")
    if os.path.exists(sg):
        try:
            f = ImageFont.truetype(sg, size)
            f.set_variation_by_axes([max(300, min(700, weight))])
        except Exception:
            f = None
    if f is None:
        for path in ("/usr/share/fonts/truetype/lato/Lato-Black.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
            try:
                f = ImageFont.truetype(path, size)
                break
            except OSError:
                continue
    _fonts[key] = f or ImageFont.load_default()
    return _fonts[key]


def fit(d, text, size, max_w, weight=700, min_size=9):
    """Largest font <= size that fits text in max_w."""
    while size > min_size and d.textlength(text, font=font(size, weight)) > max_w:
        size -= 1
    return font(size, weight)


def mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


# -- surfaces ---------------------------------------------------------------------
def card(img, box, accent, active=True, radius=20):
    """Glass card: vertical gradient, accent edge, faint top highlight, glow."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    grad = Image.new("RGB", (1, h))
    for y in range(h):
        grad.putpixel((0, y), mix(CARD_TOP, CARD_BOTTOM, y / max(1, h - 1)))
    grad = grad.resize((w, h))
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=255)
    if active:                                   # soft outer glow in the accent colour
        glow = Image.new("RGBA", img.size, (0, 0, 0, 0))
        ImageDraw.Draw(glow).rounded_rectangle(box, radius=radius, outline=accent + (120,), width=6 * SS // 3)
        glow = glow.filter(ImageFilter.GaussianBlur(5 * SS))
        img.alpha_composite(glow) if img.mode == "RGBA" else img.paste(glow, (0, 0), glow)
    img.paste(grad, (x0, y0), mask)
    d = ImageDraw.Draw(img, "RGBA")
    edge = accent + ((230,) if active else (90,))
    d.rounded_rectangle(box, radius=radius, outline=edge, width=max(2, 2 * SS // 2))
    d.line((x0 + radius, y0 + 2 * SS // 2, x1 - radius, y0 + 2 * SS // 2), fill=(255, 255, 255, 28), width=SS)
    return d


def glow_layer(img, draw_fn, color, blur=4):
    """Draw a glyph twice: blurred underneath (glow), sharp on top."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    draw_fn(ImageDraw.Draw(layer), color + (255,))
    halo = layer.filter(ImageFilter.GaussianBlur(blur * SS))
    img.alpha_composite(halo)
    img.alpha_composite(layer)


# -- glyphs: drawn in a box (cx, cy, size) at supersampled scale --------------------
def g_bot(d, c, cx, cy, s):
    w = s * 0.9
    d.rounded_rectangle((cx - w / 2, cy - s * 0.32, cx + w / 2, cy + s * 0.38), radius=s * 0.18, outline=c, width=int(s * 0.09))
    for ex in (-0.2, 0.2):
        d.ellipse((cx + ex * s - s * 0.08, cy - s * 0.05, cx + ex * s + s * 0.08, cy + s * 0.11), fill=c)
    d.line((cx, cy - s * 0.32, cx, cy - s * 0.48), fill=c, width=int(s * 0.07))
    d.ellipse((cx - s * 0.07, cy - s * 0.58, cx + s * 0.07, cy - s * 0.44), fill=c)


def g_mic(d, c, cx, cy, s):
    d.rounded_rectangle((cx - s * 0.16, cy - s * 0.5, cx + s * 0.16, cy + s * 0.12), radius=s * 0.16, fill=c)
    d.arc((cx - s * 0.32, cy - s * 0.25, cx + s * 0.32, cy + s * 0.3), 0, 180, fill=c, width=int(s * 0.08))
    d.line((cx, cy + s * 0.3, cx, cy + s * 0.48), fill=c, width=int(s * 0.08))
    d.line((cx - s * 0.18, cy + s * 0.48, cx + s * 0.18, cy + s * 0.48), fill=c, width=int(s * 0.08))


def g_bars(d, c, cx, cy, s):
    hs = (0.3, 0.62, 0.95, 0.62, 0.3)
    bw = s * 0.12
    for i, h in enumerate(hs):
        x = cx + (i - 2) * s * 0.2
        d.rounded_rectangle((x - bw / 2, cy - s * h / 2, x + bw / 2, cy + s * h / 2), radius=bw / 2, fill=c)


def g_dots(d, c, cx, cy, s):
    for i in range(3):
        x = cx + (i - 1) * s * 0.34
        r = s * (0.1 + 0.03 * (i == 1))
        d.ellipse((x - r, cy - r, x + r, cy + r), fill=c)


def g_alert(d, c, cx, cy, s):
    d.polygon([(cx, cy - s * 0.5), (cx + s * 0.5, cy + s * 0.4), (cx - s * 0.5, cy + s * 0.4)], outline=c, width=int(s * 0.08))
    d.line((cx, cy - s * 0.15, cx, cy + s * 0.12), fill=c, width=int(s * 0.09))
    d.ellipse((cx - s * 0.06, cy + s * 0.2, cx + s * 0.06, cy + s * 0.32), fill=c)


def g_unlink(d, c, cx, cy, s):
    w = int(s * 0.09)
    d.arc((cx - s * 0.55, cy - s * 0.3, cx - s * 0.02, cy + s * 0.3), 100, 260, fill=c, width=w)
    d.arc((cx + s * 0.02, cy - s * 0.3, cx + s * 0.55, cy + s * 0.3), 280, 80, fill=c, width=w)
    d.line((cx - s * 0.3, cy + s * 0.45, cx + s * 0.3, cy - s * 0.45), fill=c, width=w)


def g_stop(d, c, cx, cy, s):
    d.rounded_rectangle((cx - s * 0.36, cy - s * 0.36, cx + s * 0.36, cy + s * 0.36), radius=s * 0.1, fill=c)


def g_power(d, c, cx, cy, s):
    w = int(s * 0.1)
    d.arc((cx - s * 0.45, cy - s * 0.4, cx + s * 0.45, cy + s * 0.5), 300, 240, fill=c, width=w)
    d.line((cx, cy - s * 0.52, cx, cy + s * 0.02), fill=c, width=w)


def g_flame(d, c, cx, cy, s):
    d.polygon([(cx, cy - s * 0.55), (cx + s * 0.28, cy - s * 0.12), (cx + s * 0.34, cy + s * 0.18),
               (cx + s * 0.22, cy + s * 0.42), (cx, cy + s * 0.52), (cx - s * 0.22, cy + s * 0.42),
               (cx - s * 0.34, cy + s * 0.18), (cx - s * 0.22, cy - s * 0.08), (cx - s * 0.08, cy + s * 0.02)], fill=c)
    d.polygon([(cx, cy + s * 0.02), (cx + s * 0.13, cy + s * 0.25), (cx + s * 0.08, cy + s * 0.42),
               (cx, cy + s * 0.46), (cx - s * 0.08, cy + s * 0.42), (cx - s * 0.13, cy + s * 0.25)], fill=(255, 236, 170, 255))


def g_bulb(d, c, cx, cy, s):
    d.ellipse((cx - s * 0.3, cy - s * 0.5, cx + s * 0.3, cy + s * 0.12), outline=c, width=int(s * 0.09))
    d.rectangle((cx - s * 0.14, cy + s * 0.12, cx + s * 0.14, cy + s * 0.3), fill=c)
    d.line((cx - s * 0.14, cy + s * 0.4, cx + s * 0.14, cy + s * 0.4), fill=c, width=int(s * 0.07))
    for a, b in ((-0.55, -0.32), (0.55, -0.32), (0, -0.66)):
        d.ellipse((cx + a * s - s * 0.04, cy + b * s - s * 0.04, cx + a * s + s * 0.04, cy + b * s + s * 0.04), fill=c)


def g_hdmi(d, c, cx, cy, s):
    w = int(s * 0.08)
    d.polygon([(cx - s * 0.5, cy - s * 0.28), (cx + s * 0.5, cy - s * 0.28), (cx + s * 0.4, cy + s * 0.08),
               (cx - s * 0.4, cy + s * 0.08)], outline=c, width=w)
    for i in range(5):
        x = cx + (i - 2) * s * 0.15
        d.rectangle((x - s * 0.03, cy - s * 0.16, x + s * 0.03, cy - s * 0.04), fill=c)
    d.line((cx, cy + s * 0.2, cx, cy + s * 0.46), fill=c, width=w)
    d.polygon([(cx - s * 0.14, cy + s * 0.36), (cx + s * 0.14, cy + s * 0.36), (cx, cy + s * 0.56)], fill=c)


def g_speaker(d, c, cx, cy, s):
    d.polygon([(cx - s * 0.45, cy - s * 0.15), (cx - s * 0.25, cy - s * 0.15), (cx, cy - s * 0.4), (cx, cy + s * 0.4),
               (cx - s * 0.25, cy + s * 0.15), (cx - s * 0.45, cy + s * 0.15)], fill=c)
    for r in (0.22, 0.4):
        d.arc((cx - s * r, cy - s * r, cx + s * r * 1.6, cy + s * r), 300, 60, fill=c, width=int(s * 0.07))


def g_box(d, c, cx, cy, s):              # container
    w = int(s * 0.08)
    d.polygon([(cx, cy - s * 0.5), (cx + s * 0.45, cy - s * 0.25), (cx + s * 0.45, cy + s * 0.27),
               (cx, cy + s * 0.52), (cx - s * 0.45, cy + s * 0.27), (cx - s * 0.45, cy - s * 0.25)], outline=c, width=w)
    d.line((cx - s * 0.45, cy - s * 0.25, cx, cy, cx + s * 0.45, cy - s * 0.25), fill=c, width=w, joint="curve")
    d.line((cx, cy, cx, cy + s * 0.52), fill=c, width=w)


def g_pi(d, c, cx, cy, s):               # a small board with a chip
    w = int(s * 0.08)
    d.rounded_rectangle((cx - s * 0.5, cy - s * 0.34, cx + s * 0.5, cy + s * 0.34), radius=s * 0.08, outline=c, width=w)
    d.rectangle((cx - s * 0.16, cy - s * 0.14, cx + s * 0.16, cy + s * 0.14), fill=c)
    for i in range(5):
        x = cx - s * 0.36 + i * s * 0.18
        d.ellipse((x - s * 0.03, cy - s * 0.26, x + s * 0.03, cy - s * 0.2), fill=c)


def g_home(d, c, cx, cy, s):
    w = int(s * 0.09)
    d.line((cx - s * 0.5, cy - s * 0.02, cx, cy - s * 0.48, cx + s * 0.5, cy - s * 0.02), fill=c, width=w, joint="curve")
    d.rounded_rectangle((cx - s * 0.34, cy - s * 0.1, cx + s * 0.34, cy + s * 0.46), radius=s * 0.06, outline=c, width=w)
    d.rectangle((cx - s * 0.09, cy + s * 0.16, cx + s * 0.09, cy + s * 0.46), fill=c)


def g_laptop(d, c, cx, cy, s):
    w = int(s * 0.08)
    d.rounded_rectangle((cx - s * 0.36, cy - s * 0.4, cx + s * 0.36, cy + s * 0.16), radius=s * 0.06, outline=c, width=w)
    d.rounded_rectangle((cx - s * 0.52, cy + s * 0.24, cx + s * 0.52, cy + s * 0.34), radius=s * 0.05, fill=c)


def g_cloud(d, c, cx, cy, s):
    for (x, y, r) in ((-0.22, 0.06, 0.24), (0.06, -0.08, 0.3), (0.3, 0.1, 0.2)):
        d.ellipse((cx + (x - r) * s, cy + (y - r) * s, cx + (x + r) * s, cy + (y + r) * s), fill=c)
    d.rounded_rectangle((cx - s * 0.46, cy + s * 0.06, cx + s * 0.5, cy + s * 0.3), radius=s * 0.12, fill=c)


def g_globe(d, c, cx, cy, s):
    w = int(s * 0.07)
    d.ellipse((cx - s * 0.45, cy - s * 0.45, cx + s * 0.45, cy + s * 0.45), outline=c, width=w)
    d.ellipse((cx - s * 0.2, cy - s * 0.45, cx + s * 0.2, cy + s * 0.45), outline=c, width=w)
    d.line((cx - s * 0.45, cy, cx + s * 0.45, cy), fill=c, width=w)


GLYPHS = {"box": g_box, "pi": g_pi, "home": g_home, "laptop": g_laptop, "cloud": g_cloud, "globe": g_globe,
          "bot": g_bot, "mic": g_mic, "bars": g_bars, "dots": g_dots, "alert": g_alert, "unlink": g_unlink,
          "stop": g_stop, "power": g_power, "flame": g_flame, "bulb": g_bulb, "hdmi": g_hdmi, "speaker": g_speaker}


# -- key icons ----------------------------------------------------------------------
def key_icon(caption, label, glyph, accent, active=True, dot=None, size=112):
    """One deck key: small caption top-left, glowing glyph, one big word."""
    S = size * SS
    acc = ACCENT.get(accent, accent)
    img = Image.new("RGBA", (S, S), BG + (255,))
    m = 4 * SS
    d = card(img, (m, m, S - m, S - m), acc, active=active, radius=20 * SS)
    d.text((14 * SS, 11 * SS), caption, font=fit(d, caption, 12 * SS, S - 40 * SS, 600), fill=acc + (255,) if active else DIM + (255,))
    if dot:
        dc = ACCENT.get(dot, dot)
        r = 5 * SS
        d.ellipse((S - 20 * SS - r, 17 * SS - r, S - 20 * SS + r, 17 * SS + r), fill=dc + (255,))
    gcol = acc if active else DIM
    glow_layer(img, lambda dd, col: GLYPHS[glyph](dd, col, S / 2, S * 0.45, 38 * SS), gcol, blur=3 if active else 0.01)
    d = ImageDraw.Draw(img, "RGBA")
    f = fit(d, label, 21 * SS, S - 22 * SS, 700)
    w = d.textlength(label, font=f)
    d.text(((S - w) / 2, S * 0.70), label, font=f, fill=TEXT + (255,) if active else DIM + (255,))
    return img.convert("RGB").resize((size, size), Image.LANCZOS)


def gauge_icon(caption, value, sub, frac, accent, dot=None, size=112):
    """A live stat key: caption, a ring gauge with the value inside, one sub line."""
    S = size * SS
    acc = ACCENT.get(accent, accent)
    img = Image.new("RGBA", (S, S), BG + (255,))
    m = 4 * SS
    d = card(img, (m, m, S - m, S - m), acc, active=True, radius=20 * SS)
    d.text((14 * SS, 11 * SS), caption, font=fit(d, caption, 12 * SS, S - 40 * SS, 600), fill=acc + (255,))
    if dot:
        dc, r = ACCENT.get(dot, dot), 5 * SS
        d.ellipse((S - 20 * SS - r, 17 * SS - r, S - 20 * SS + r, 17 * SS + r), fill=dc + (255,))
    cx, cy, r, w = S / 2, S * 0.53, 30 * SS, 6 * SS
    box = (cx - r, cy - r, cx + r, cy + r)
    d.arc(box, 135, 405, fill=(36, 40, 56, 255), width=w)            # 270 degree track
    if frac is not None and frac > 0:
        end = 135 + 270 * max(0.02, min(1.0, frac))
        glow_layer(img, lambda dd, col: dd.arc(box, 135, end, fill=col, width=w), acc, blur=2)
    d = ImageDraw.Draw(img, "RGBA")
    f = fit(d, value, 20 * SS, 2 * r - 12 * SS, 700)
    tw = d.textlength(value, font=f)
    d.text((cx - tw / 2, cy - 12 * SS), value, font=f, fill=TEXT + (255,))
    if sub:
        f2 = fit(d, sub, 11 * SS, S - 22 * SS, 600)
        tw = d.textlength(sub, font=f2)
        d.text(((S - tw) / 2, S - 25 * SS), sub, font=f2, fill=DIM + (255,))
    return img.convert("RGB").resize((size, size), Image.LANCZOS)


def status_icon(caption, word, glyph, ok, sub="", size=112, accent=None):
    """A device key: green when up, red when down, with a short detail (latency)."""
    accent = accent or ("green" if ok else "red" if ok is False else "slate")
    S = size * SS
    acc = ACCENT[accent]
    img = Image.new("RGBA", (S, S), BG + (255,))
    m = 4 * SS
    d = card(img, (m, m, S - m, S - m), acc, active=ok is not None, radius=20 * SS)
    d.text((14 * SS, 11 * SS), caption, font=fit(d, caption, 12 * SS, S - 40 * SS, 600), fill=acc + (255,))
    r = 5 * SS
    d.ellipse((S - 20 * SS - r, 17 * SS - r, S - 20 * SS + r, 17 * SS + r), fill=acc + (255,))
    glow_layer(img, lambda dd, col: GLYPHS[glyph](dd, col, S / 2, S * 0.39, 30 * SS), acc if ok else DIM,
               blur=3 if ok else 0.01)
    d = ImageDraw.Draw(img, "RGBA")
    f = fit(d, word, 18 * SS, S - 22 * SS, 700)
    tw = d.textlength(word, font=f)
    d.text(((S - tw) / 2, S * 0.585), word, font=f, fill=TEXT + (255,) if ok else acc + (255,))
    if sub:
        f2 = fit(d, sub, 10 * SS, S - 22 * SS, 600)
        tw = d.textlength(sub, font=f2)
        d.text(((S - tw) / 2, S - 21 * SS), sub, font=f2, fill=DIM + (255,))
    return img.convert("RGB").resize((size, size), Image.LANCZOS)


# -- live strip -----------------------------------------------------------------------
class Strip:
    """The 704x124 strip as four cards above the knobs, at 3x."""
    W, H, PW = 704, 124, 176

    def __init__(self):
        self.img = Image.new("RGBA", (self.W * SS, self.H * SS), BG + (255,))

    def panel(self, i, title, accent, hint="", active=True, glyph=None):
        acc = ACCENT.get(accent, accent)
        x0 = i * self.PW * SS
        box = (x0 + 4 * SS, 4 * SS, x0 + (self.PW - 4) * SS, (self.H - 4) * SS)
        d = card(self.img, box, acc, active=active, radius=16 * SS)
        tx = x0 + 14 * SS
        if glyph:
            glow_layer(self.img, lambda dd, col: GLYPHS[glyph](dd, col, tx + 6 * SS, 18 * SS, 11 * SS), acc, blur=1)
            tx += 18 * SS
        d = ImageDraw.Draw(self.img, "RGBA")
        d.text((tx, 10 * SS), title, font=font(12 * SS, 700), fill=acc + (255,))
        if hint:
            d.text((x0 + 14 * SS, (self.H - 22) * SS), hint, font=font(10 * SS, 500), fill=DIM + (255,))
        return d, x0

    def big(self, i, text, color, y=30, size=40, weight=700):
        d = ImageDraw.Draw(self.img, "RGBA")
        x0 = i * self.PW * SS
        f = fit(d, text, size * SS, (self.PW - 28) * SS, weight)
        d.text((x0 + 14 * SS, y * SS), text, font=f, fill=ACCENT.get(color, color) + (255,))

    def small(self, i, text, y, color=TEXT, size=12, weight=600):
        d = ImageDraw.Draw(self.img, "RGBA")
        x0 = i * self.PW * SS
        f = fit(d, text, size * SS, (self.PW - 28) * SS, weight)
        d.text((x0 + 14 * SS, y * SS), text, font=f, fill=color + (255,))

    def bar(self, i, frac, accent, y=86, x=14, w=148, h=8):
        d = ImageDraw.Draw(self.img, "RGBA")
        acc = ACCENT.get(accent, accent)
        x0 = i * self.PW * SS + x * SS
        d.rounded_rectangle((x0, y * SS, x0 + w * SS, (y + h) * SS), radius=h * SS // 2, fill=(36, 40, 56, 255))
        if frac and frac > 0:
            fw = max(h, int(w * min(1.0, frac))) * SS
            bar = Image.new("RGBA", (fw, h * SS))
            for xx in range(fw):                        # gradient fill, brighter at the head
                c = mix(mix(acc, BG, 0.35), acc, xx / max(1, fw - 1))
                ImageDraw.Draw(bar).line((xx, 0, xx, h * SS), fill=c + (255,))
            mask = Image.new("L", bar.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, fw - 1, h * SS - 1), radius=h * SS // 2, fill=255)
            self.img.paste(bar, (x0, y * SS), mask)

    def text_block(self, i, text, y=28, size=12, lines=5, color=TEXT, width=None):
        d = ImageDraw.Draw(self.img, "RGBA")
        x0 = i * self.PW * SS + 14 * SS
        f = font(size * SS, 500)
        maxw = (width or (self.PW - 28)) * SS
        words, out, cur = text.split(), [], ""
        for w in words:
            t = (cur + " " + w).strip()
            if d.textlength(t, font=f) <= maxw:
                cur = t
            else:
                out.append(cur)
                cur = w
                if len(out) == lines:
                    break
        if cur and len(out) < lines:
            out.append(cur)
        if len(out) == lines and " ".join(out) != " ".join(words):
            out[-1] = out[-1].rstrip(".,") + "…"
        for j, line in enumerate(out):
            d.text((x0, (y + j * (size + 3)) * SS), line, font=f, fill=color + (255,))

    def spark(self, i, values, accent, y=40, h=44, x=14, w=148, vmax=None, fill=True):
        """Area sparkline of recent values (oldest first)."""
        vals = [v for v in values if v is not None]
        if len(vals) < 2:
            return
        acc = ACCENT.get(accent, accent)
        top = vmax or max(max(vals), 1e-9)
        x0 = i * self.PW * SS + x * SS
        pts = [(x0 + j * w * SS / (len(vals) - 1), (y + h) * SS - min(1.0, v / top) * h * SS)
               for j, v in enumerate(vals)]
        layer = Image.new("RGBA", self.img.size, (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer)
        if fill:
            ld.polygon(pts + [(pts[-1][0], (y + h) * SS), (pts[0][0], (y + h) * SS)], fill=acc + (46,))
        ld.line(pts, fill=acc + (255,), width=2 * SS, joint="curve")
        self.img.alpha_composite(layer)
        d = ImageDraw.Draw(self.img, "RGBA")
        ex, ey = pts[-1]
        d.ellipse((ex - 3 * SS, ey - 3 * SS, ex + 3 * SS, ey + 3 * SS), fill=acc + (255,))

    def image(self):
        return self.img.convert("RGB").resize((self.W, self.H), Image.LANCZOS)
