"""Pens: the colour palette, HEX/RGB parsing and the tiny markup that lets one document use many pen colours.

How colours travel through the app
----------------------------------
The editable text is plain text, one paragraph per line.  A colour is attached to text with a *pen tag*:

    {red} Whole line in red                     tag at the very start of a line (and no {/} later) = whole line
    Some {green}words{/} in green               {pen} ... {/} = just the text between the tags
    ## {black} A heading in black               the '#' heading marker always comes first
    {important} Roles resolve to the pen chosen in "Pen settings" (body, heading, important, definition, highlight)

A pen is a preset name (black, blue, red, green, purple, orange, pink, brown, darkblue), a role name,
``#rgb`` / ``#rrggbb`` or ``rgb(r,g,b)``.  Anything else in braces (e.g. ``{hello}``) is ordinary text.
Because tags live inside the text, they are saved with the document, survive every edit, and reach the renderer
that draws both the live preview and the PDF - so the colours can never get lost between the two.
"""
import re

# name -> RGB.  The first eight are shown in the palette (in this order); darkblue is kept for old clients.
PEN_COLORS = {
    "black": (24, 24, 30), "blue": (28, 62, 190), "red": (192, 30, 42), "green": (22, 128, 62),
    "purple": (124, 58, 190), "orange": (230, 120, 20), "pink": (222, 64, 140), "brown": (110, 66, 34),
    "darkblue": (14, 24, 108),
}
PALETTE_ORDER = ("black", "blue", "red", "green", "purple", "orange", "pink", "brown")

# Roles = named jobs a pen can do.  "body" and "heading" are used automatically; the others are applied with tags.
ROLES = ("body", "heading", "important", "definition", "highlight")
ROLE_DEFAULTS = {"important": "red", "definition": "green", "highlight": "purple"}

_HEX = re.compile(r"^#([0-9a-f]{3}|[0-9a-f]{6})$")
_RGB = re.compile(r"^rgb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)$")
_TAG = re.compile(r"\{(/|[A-Za-z]{3,10}|#[0-9a-fA-F]{6}|#[0-9a-fA-F]{3}|rgb\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*\))\}")


def parse_pen(value):
    """Preset name, #hex, rgb(r,g,b) or an (r,g,b) sequence -> (r, g, b) tuple, or None if it isn't a colour."""
    if isinstance(value, (tuple, list)) and len(value) == 3:
        try: return tuple(max(0, min(255, int(v))) for v in value)
        except (TypeError, ValueError): return None
    if not isinstance(value, str): return None
    s = value.strip().lower()
    if s in PEN_COLORS: return PEN_COLORS[s]
    m = _HEX.match(s)
    if m:
        h = m.group(1)
        if len(h) == 3: h = "".join(c * 2 for c in h)
        return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    m = _RGB.match(s)
    if m:
        rgb = tuple(int(g) for g in m.groups())
        return rgb if max(rgb) <= 255 else None
    return None


def to_hex(rgb):
    return "#%02x%02x%02x" % tuple(rgb)


def build_roles(o):
    """Options -> {role: (r, g, b)}.  The heading pen follows the body pen unless one was chosen explicitly."""
    body = parse_pen(o.get("ink")) or PEN_COLORS["blue"]
    given = o.get("pens") if isinstance(o.get("pens"), dict) else {}
    roles = {"body": body, "heading": parse_pen(given.get("heading")) or body}
    for role, default in ROLE_DEFAULTS.items():
        roles[role] = parse_pen(given.get(role)) or PEN_COLORS[default]
    return roles


def _tags(text):
    """(start, end, token) for every *valid* tag in the line; unknown {words} stay ordinary text."""
    found = []
    for m in _TAG.finditer(text):
        tok = m.group(1).lower() if m.group(1) != "/" else "/"
        if tok == "/" or tok in ROLES or parse_pen(tok) is not None:
            found.append((m.start(), m.end(), tok))
    return found


def parse_line(text):
    """One line of editor text -> [(text, token or None), ...].  token None = use the block's own pen."""
    tags = _tags(text)
    line_pen, pos, lead = None, 0, len(text) - len(text.lstrip())
    if tags and tags[0][0] <= lead and tags[0][2] != "/" and all(t[2] != "/" for t in tags):
        line_pen, pos, tags = tags[0][2], tags[0][1], tags[1:]      # whole-line pen
    runs, cur = [], line_pen
    for s, e, tok in tags:
        if s > pos: runs.append((text[pos:s], cur))
        cur, pos = (line_pen if tok == "/" else tok), e
    if pos < len(text): runs.append((text[pos:], cur))
    return runs


def strip_tags(text):
    """The text as it will be written on the page (no pen tags)."""
    return "".join(t for t, _ in parse_line(text))


def resolve(token, roles):
    """Pen token -> RGB using the role table, or None."""
    return roles.get(token) if token in ROLES else parse_pen(token)


def used_pens(blocks, o):
    """Ordered, de-duplicated list of '#rrggbb' colours that actually appear in the document (for the UI legend)."""
    roles, seen = build_roles(o), []
    for b in blocks:
        plain = strip_tags(b["text"]).strip()
        if not plain: continue
        base = roles["heading"] if b["level"] else roles["body"]
        for text, tok in parse_line(b["text"]):
            if not text.strip(): continue
            rgb = (resolve(tok, roles) if tok else None) or base
            h = to_hex(rgb)
            if h not in seen: seen.append(h)
    return seen
