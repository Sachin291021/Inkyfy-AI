"""Renders text blocks onto paper images so they look handwritten: per-word/char jitter,
baseline wobble, slight rotation, ink-pressure variation, and text aligned to ruled lines."""
import random
import re
from functools import lru_cache
from pathlib import Path
import urllib.request
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from .pens import PEN_COLORS, build_roles, parse_line, resolve

DPI = 120
FONT_DIR = Path(__file__).resolve().parent.parent / "static" / "fonts"
# style -> (file, connected script (render per word), size correction or None = auto-match x-height)
STYLES = {
    "student": ("PatrickHand-Regular.ttf", False, 1.0),
    "cursive": ("HomemadeApple-Regular.ttf", True, 0.72),
    "casual": ("IndieFlower-Regular.ttf", False, 1.0),
    "elegant": ("Sacramento-Regular.ttf", True, 1.3),
    "print": ("ArchitectsDaughter-Regular.ttf", False, 0.95),
    # more human-looking pens (size auto-matched)
    "natural": ("Handlee-Regular.ttf", False, None),
    "notes": ("Kalam-Regular.ttf", False, None),
    "realpen": ("NothingYouCouldDo.ttf", False, None),
    "flowing": ("LaBelleAurore.ttf", True, ("auto", 1.1)),
    "scribble": ("Zeyada.ttf", True, None),
    "thinpen": ("WaitingfortheSunrise.ttf", False, ("auto", 1.3)),
    "pencil": ("ShadowsIntoLight.ttf", False, ("auto", 1.3)),
    "scrawl": ("ReenieBeanie.ttf", False, ("auto", 1.3)),
    "schoolkid": ("Schoolbell-Regular.ttf", False, None),
    "marker": ("CoveredByYourGrace.ttf", False, ("auto", 1.45)),
    "compact": ("NanumPenScript-Regular.ttf", False, ("auto", 1.15)),
    # Added realistic handwriting fonts
    "caveat": ("Caveat-Regular.ttf", False, ("auto", 1.05)),
    "caveatbrush": ("CaveatBrush-Regular.ttf", False, ("auto", 1.05)),
}
INKS = PEN_COLORS   # preset pen name -> RGB (defined in pens.py; kept here for backwards compatibility)
THICKNESS = ("thin", "medium", "thick")
HIGHLIGHT_MODES = ("off", "marked", "colored")   # off | only text tagged {highlight} | every explicitly coloured run
_THIN_LUT = [int(255 * (v / 255) ** 2.4) for v in range(256)]   # fades the anti-aliased fringe -> finer strokes
_BOLD_LUT = [min(255, int(255 * (v / 255) ** 0.7)) for v in range(256)]   # pushes anti-aliased edge pixels darker
_GROW_LUT = [int(v * 0.42) for v in range(256)]                 # soft 1px halo around every stroke -> bolder ink
PAPERS = ("ruled", "plain", "cream", "grid")
SIZES_MM = {"A4": (210, 297), "Letter": (215.9, 279.4)}
HEADING_SCALE = {0: 1.0, 1: 1.4, 2: 1.2, 3: 1.1}
MAX_PAGES = 60


# These fonts are bundled when available. If a deployment omitted them, we try
# to fetch them once and then fall back to a visually similar bundled font so
# selecting the style never breaks conversion.
FONT_DOWNLOADS = {
    # Caveat is currently published as a variable TTF in Google Fonts.
    "Caveat-Regular.ttf": [
        "https://raw.githubusercontent.com/google/fonts/main/ofl/caveat/Caveat%5Bwght%5D.ttf",
        "https://raw.githubusercontent.com/google/fonts/main/ofl/caveat/Caveat-Regular.ttf",
    ],
    "CaveatBrush-Regular.ttf": [
        "https://raw.githubusercontent.com/google/fonts/main/ofl/caveatbrush/CaveatBrush-Regular.ttf",
    ],
}
FONT_FALLBACKS = {
    "Caveat-Regular.ttf": "Handlee-Regular.ttf",
    "CaveatBrush-Regular.ttf": "CoveredByYourGrace.ttf",
}

def _ensure_font(name):
    path = FONT_DIR / name
    if path.exists():
        return path

    # Download the requested font when the server has internet access.
    urls = FONT_DOWNLOADS.get(name, [])
    if isinstance(urls, str):
        urls = [urls]
    if urls:
        FONT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        for url in urls:
            try:
                urllib.request.urlretrieve(url, tmp)
                if tmp.exists() and tmp.stat().st_size > 1000:
                    tmp.replace(path)
                    break
            except Exception:
                pass
            finally:
                try: tmp.unlink(missing_ok=True)
                except Exception: pass

    if path.exists():
        return path

    # The ZIP distributed with the app may not contain newly added fonts.
    # Use a bundled handwriting font as a local fallback instead of returning
    # a 500 error. The style key remains Caveat/Caveat Brush, and deployments
    # with internet access will use the real font automatically.
    fallback = FONT_FALLBACKS.get(name)
    if fallback:
        fallback_path = FONT_DIR / fallback
        if fallback_path.exists():
            return fallback_path

    raise FileNotFoundError(
        f"Handwriting font '{name}' is missing and no fallback is available."
    )

@lru_cache(maxsize=64)
def _font(name, px):
    return ImageFont.truetype(str(_ensure_font(name)), px)


@lru_cache(maxsize=200_000)
def _adv_cached(name, px, text):
    return _font(name, px).getlength(text)


def _adv(font, text):
    """font.getlength() with a cache (the same few characters are measured tens of thousands of times)."""
    return _adv_cached(Path(font.path).name, font.size, text)


_MASKS = {}


def _base_mask(font, text, thick="medium"):
    """Glyph mask + ascent, rendered once per (font, size, text, pen thickness) and reused (never mutated)."""
    key = (font.path, font.size, text, thick)
    hit = _MASKS.get(key)
    if hit is None:
        asc, desc = font.getmetrics()
        mask = Image.new("L", (int(_adv(font, text)) + 8, asc + desc + 8), 0)
        ImageDraw.Draw(mask).text((4, 4), text, font=font, fill=255)
        if thick == "thick":
            halo = mask.filter(ImageFilter.MaxFilter(3)).point(_GROW_LUT)
            mask = ImageChops.lighter(mask.point(_BOLD_LUT), halo)
        elif thick == "thin":
            mask = mask.point(_THIN_LUT)
        if len(_MASKS) > 4000: _MASKS.clear()
        hit = _MASKS[key] = (mask, asc)
    return hit


@lru_cache(maxsize=32)
def _auto_corr(name):
    """Scale a font so its lowercase x-height matches the reference font (all fonts then look the same size)."""
    def xh(n):
        b = ImageFont.truetype(str(_ensure_font(n)), 200).getbbox("x")
        return (b[3] - b[1]) / 200
    return max(0.6, min(1.8, xh("PatrickHand-Regular.ttf") / xh(name)))


def _mm(v):
    return int(v / 25.4 * DPI)


def _paper(kind, w, h, top, pitch, lm):
    im = Image.new("RGB", (w, h), (251, 244, 225) if kind == "cream" else (255, 255, 255))
    d = ImageDraw.Draw(im)
    if kind == "ruled":
        for y in range(top, h - 20, pitch):
            d.line([(0, y), (w, y)], fill=(170, 200, 235), width=1)
        d.line([(lm - 16, 0), (lm - 16, h)], fill=(236, 140, 140), width=2)
    elif kind == "grid":
        step = int(DPI * 0.2)
        for x in range(0, w, step): d.line([(x, 0), (x, h)], fill=(205, 220, 235), width=1)
        for y in range(0, h, step): d.line([(0, y), (w, y)], fill=(205, 220, 235), width=1)
    return im


def _glyph(page, text, font, ink, x, base, dy, rot, opacity, thick="medium"):
    """Draw one word/char as a mask, rotate it slightly, and paste it in ink colour."""
    src, asc = _base_mask(font, text, thick)
    w, h = src.size
    fill = int(255 * opacity)
    mask = src if fill >= 255 else src.point([(v * fill + 127) // 255 for v in range(256)])
    if abs(rot) > 0.05:
        mask = mask.rotate(rot, resample=Image.BICUBIC, expand=True)
    page.paste(ink, (int(x - 4 - (mask.width - w) / 2), int(base - asc - 4 + dy - (mask.height - h) / 2)), mask)


# A "segment" is a run of text in one pen: (text, (r, g, b), highlighted).  A "word" is a list of segments
# (normally one; several only when a pen tag starts or ends in the middle of a word).
def _segments(text, base_rgb, roles, mode):
    """Block text (with pen tags) -> segments.  Untagged text uses the block's own pen (body or heading)."""
    body, segs = roles["body"], []
    for t, tok in parse_line(text):
        rgb = (resolve(tok, roles) if tok else None) or base_rgb
        explicit = tok is not None and resolve(tok, roles) is not None
        hl = explicit and ((tok == "highlight" and mode != "off") or (mode == "colored" and rgb != body))
        segs.append((t, rgb, hl))
    return segs


def _words(segs):
    words, cur = [], []
    for text, rgb, hl in segs:
        for part in re.split(r"(\s+)", text):
            if not part: continue
            if part.isspace():
                if cur: words.append(cur); cur = []
            else:
                cur.append((part, rgb, hl))
    if cur: words.append(cur)
    return words


def _word_w(font, word):
    return sum(_adv(font, s[0]) for s in word)


def _split_long(word, font, max_w):
    """Break a word wider than the line into fitting chunks (every character keeps its pen)."""
    out, cur, cur_txt = [], [], ""
    for text, rgb, hl in word:
        for ch in text:
            if cur_txt and _adv(font, cur_txt + ch) > max_w:
                out.append(cur); cur, cur_txt = [], ""
            cur_txt += ch
            if cur and cur[-1][1:] == (rgb, hl): cur[-1] = (cur[-1][0] + ch, rgb, hl)
            else: cur.append((ch, rgb, hl))
    if cur: out.append(cur)
    return out


def _shade(rgb, f):
    return tuple(max(0, min(255, int(c * f))) for c in rgb)


def _band(page, x0, x1, base, font, pitch, rgb):
    """Marker-pen highlight: a translucent band in the pen colour, multiplied onto the page so ink stays readable."""
    asc, desc = font.getmetrics()
    top, bot = int(base - asc * 0.82), int(base + desc * 0.55) + 2
    if bot - top > pitch - 2: top = bot - (pitch - 2)
    box = (max(0, int(x0) - 3), max(0, top), min(page.width, int(x1) + 3), min(page.height, bot))
    if box[2] - box[0] < 2 or box[3] - box[1] < 2: return
    region = page.crop(box)
    luma = 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]
    k = 0.12 if luma < 70 else 0.27                                  # very dark pens get a lighter band
    tint = Image.new("RGB", region.size, tuple(255 - int((255 - c) * k) for c in rgb))
    shape = Image.new("L", region.size, 0)
    ImageDraw.Draw(shape).rounded_rectangle([0, 0, region.width - 1, region.height - 1], radius=min(6, region.height // 4), fill=255)
    page.paste(ImageChops.multiply(region, tint), box[:2], shape)


def iter_pages(blocks, o):
    """blocks: from document_reader (text may contain pen tags, see pens.py). o: validated options dict.
    Generator yielding finished PIL RGB pages one at a time, so a long document never has to sit in memory all at
    once. The page limit is checked up-front (ValueError is raised before the first page is produced)."""
    fname, connected, corr = STYLES[o["style"]]
    corr = _auto_corr(fname) if corr is None else (_auto_corr(fname) * corr[1] if isinstance(corr, tuple) else corr)
    roles, irr = build_roles(o), o["irregularity"]
    thick = o.get("thickness") if o.get("thickness") in THICKNESS else "medium"
    intensity = max(0.3, min(1.0, float(o.get("ink_intensity", 100)) / 100))
    var = max(0.0, min(10.0, float(o.get("ink_variation", 3))))
    mode = o.get("highlight") if o.get("highlight") in HIGHLIGHT_MODES else "off"
    lo = max(0.25, 1 - 0.14 * var / 3)                  # lowest per-word ink pressure (var 3 = the original 0.86)
    W, H = (_mm(v) for v in SIZES_MM[o["page_size"]])
    m = _mm(o["margin"]); lm = m + _mm(o["left_margin"]); top = m
    pitch = max(12, int(o["font_size"] * DPI / 72 * o["line_spacing"]))
    usable = W - lm - m
    per_page = max(1, (H - top - m) // pitch)
    rng = random.Random(42)      # fixed seed -> stable preview between renders (the sequence of draws is never changed)
    ink_rng = random.Random(7)   # separate stream for the colour-shade drift, so it can't move any glyph

    # 1) layout: wrap every block into lines of (words, font, level, space)
    lines = []
    for b in blocks:
        if not b["text"]:
            lines.append(None); continue
        base_rgb = roles["heading"] if b["level"] else roles["body"]
        px = max(8, int(o["font_size"] * DPI / 72 * corr * HEADING_SCALE[b["level"]]))
        font = _font(fname, px)
        space = max(_adv(font, " "), px * 0.3)
        words = []
        for w in _words(_segments(b["text"], base_rgb, roles, mode)):
            words.extend(_split_long(w, font, usable) if _word_w(font, w) > usable else [w])
        if not words:                       # nothing but pen tags: treat like a blank line
            lines.append(None); continue
        cur, cur_w = [], 0
        for w in words:
            ww = _word_w(font, w)
            if cur and cur_w + space + ww > usable * 0.98:
                lines.append((cur, font, b["level"], space)); cur, cur_w = [], 0
            cur_w += (space if cur else 0) + ww; cur.append(w)
        if cur: lines.append((cur, font, b["level"], space))

    # 2) how many pages will this need? (fail fast on huge documents instead of rendering for a minute first)
    idx, last = 0, -1
    for ln in lines:
        if ln is None:
            if idx % per_page != 0: idx += 1
            continue
        last = idx // per_page; idx += 1
    if last + 1 > MAX_PAGES: raise ValueError(f"Document is too long (over {MAX_PAGES} pages).")
    if last < 0:  # nothing but blank lines
        yield _paper(o["paper"], W, H, top, pitch, lm); return

    # 3) paint (a page is created only when a text line lands on it, so there is never a stray empty trailing page)
    page, cur_no, idx = None, -1, 0
    for ln in lines:
        if ln is None:
            if idx % per_page != 0: idx += 1   # blank line (skipped at top of a page)
            continue
        if idx // per_page != cur_no:
            if page is not None: yield page
            cur_no = idx // per_page
            page = _paper(o["paper"], W, H, top, pitch, lm)
        words, font, level, space = ln
        base = top + (idx % per_page + 1) * pitch - 5 + rng.gauss(0, irr * 0.3)
        total = sum(_word_w(font, w) for w in words) + space * (len(words) - 1)
        x = lm + rng.uniform(0, irr * 1.2)
        if level == 1: x = lm + max(0, (usable - total) / 2)  # centre titles
        bands, open_band = [], None
        for w in words:
            dy, op = rng.gauss(0, irr * 0.5), rng.uniform(lo, 1.0) * intensity
            f = 1 + max(-0.2, min(0.15, ink_rng.gauss(0, 0.02 * var)))   # tiny per-word shade drift of the pen colour
            if connected:  # joined scripts: rotate the whole word (all of its colour segments share one rotation)
                rot = rng.gauss(0, irr * 0.35)
                for text, rgb, hl in w:
                    x0 = x
                    _glyph(page, text, font, _shade(rgb, f), x, base, dy, rot, op, thick)
                    x += _adv(font, text)
                    if hl:
                        if open_band is not None and open_band[2] == rgb: open_band[1] = x
                        else: open_band = [x0, x, rgb]; bands.append(open_band)
                    else: open_band = None
            else:          # print-like: jitter each character
                for text, rgb, hl in w:
                    x0 = x
                    for ch in text:
                        _glyph(page, ch, font, _shade(rgb, f), x, base, dy + rng.gauss(0, irr * 0.35), rng.gauss(0, irr * 0.6), op, thick)
                        x += _adv(font, ch) + rng.gauss(0, irr * 0.25)
                    if hl:
                        if open_band is not None and open_band[2] == rgb: open_band[1] = x
                        else: open_band = [x0, x, rgb]; bands.append(open_band)
                    else: open_band = None
            x += space * rng.uniform(0.9, 1.1 + irr * 0.02)
        for x0, x1, rgb in bands: _band(page, x0, x1, base, font, pitch, rgb)
        idx += 1
    if page is not None: yield page


def render_pages(blocks, o):
    """Same as iter_pages() but returns a list (kept for callers that want everything at once)."""
    return list(iter_pages(blocks, o))
