"""Printable "My Handwriting" template (vector A4 PDF, built with ReportLab).

Contents: A-Z, a-z, 0-9, common punctuation, common symbols (one writing box per character, with baseline / x-height / cap-height
guides) and a page of sample sentences. Four black corner squares and a page code on every page are the registration marks that
Part 2 will use to straighten and crop scans automatically - so keep them when changing the layout.
"""
from functools import lru_cache
from io import BytesIO

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.lib.utils import simpleSplit
from reportlab.pdfgen import canvas

TEMPLATE_VERSION = "T1"
PW, PH = 210.0, 297.0                  # page size in mm
LEFT, RIGHT = 14.0, 196.0              # content columns (182 mm wide)
TOP, BOTTOM = 17.0, 281.0              # content area (mm from the top)
LINE_H = 12.5                          # height of one writing line for the sample sentences
COLS, CELL_W, CELL_H = 7, 26.0, 22.0   # 7 x 26 mm = 182 mm
PURPLE, GREY, LIGHT, GUIDE = (0.357, 0.169, 0.839), (0.55, 0.55, 0.6), (0.82, 0.82, 0.86), (0.62, 0.74, 0.92)

UPPER = [(c, "") for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"]
LOWER = [(c, "") for c in "abcdefghijklmnopqrstuvwxyz"]
DIGITS = [(c, "") for c in "0123456789"]
PUNCT = [(".", "period"), (",", "comma"), (";", "semicolon"), (":", "colon"), ("!", "exclamation"), ("?", "question"),
         ("'", "apostrophe"), ('"', "quote"), ("(", "open paren"), (")", "close paren"), ("[", "open bracket"),
         ("]", "close bracket"), ("{", "open brace"), ("}", "close brace"), ("-", "hyphen"), ("\u2013", "dash"),
         ("_", "underscore"), ("/", "slash"), ("\\", "backslash"), ("\u2026", "ellipsis"), ("\u2022", "bullet")]
SYMBOLS = [("@", "at"), ("#", "hash"), ("$", "dollar"), ("%", "percent"), ("^", "caret"), ("&", "ampersand"), ("*", "asterisk"),
           ("+", "plus"), ("=", "equals"), ("<", "less than"), (">", "greater than"), ("|", "pipe"), ("~", "tilde"),
           ("`", "backtick"), ("\u20ac", "euro"), ("\u00a3", "pound"), ("\u00a5", "yen"), ("\u00b0", "degree"),
           ("\u00a9", "copyright"), ("\u00a7", "section"), ("\u00d7", "multiply")]
SECTIONS = [("Uppercase letters", "A \u2013 Z", UPPER), ("Lowercase letters", "a \u2013 z", LOWER), ("Numbers", "0 \u2013 9", DIGITS),
            ("Punctuation marks", "", PUNCT), ("Symbols", "", SYMBOLS)]
SENTENCES = ["ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz", "0123456789",
             "The quick brown fox jumps over the lazy dog.", "Pack my box with five dozen liquor jugs.",
             "Hello! Is this your pen? Yes, it's mine (and so is that one).",
             "Meet me at 4:30 on 21/08/2026 - cost: $15.75 + 5% tax = $16.54 & more.",
             "Email name_surname@example.com or see #notes [draft] {v2}: 50% off *today*."]
INSTRUCTIONS = [
    "Print this sheet at 100% (\u201cActual size\u201d, not \u201cFit to page\u201d) on plain A4 paper.",
    "Write each character inside its box with a dark pen (black or dark blue). Sit letters on the solid blue line.",
    "Write naturally, at your normal speed and size \u2013 do not try to be neater than usual.",
    "Stay inside the boxes and do not write over the small grey reference character.",
    "Scan or photograph every page flat, in bright even light, with all four black corner squares visible.",
    "Upload the pages as PNG, JPG or PDF (separate images or one PDF) in My Own Handwriting.",
]


def _layout():
    """Pure layout pass -> list of pages, each a list of draw operations (positions in mm from the top-left)."""
    pages, ops, y = [], [], TOP

    def new_page():
        nonlocal ops, y
        ops = []
        pages.append(ops)
        y = TOP

    new_page()
    ops.append(("title", y))
    y += 17
    box_lines = [ln for t in INSTRUCTIONS for ln in simpleSplit(t, "Helvetica", 9, (RIGHT - LEFT - 10) * mm)]
    box_h = 12 + 4.6 * len(box_lines)
    ops.append(("instructions", y, box_h))
    y += box_h + 6

    for title, sub, items in SECTIONS:
        if y + 9 + CELL_H > BOTTOM:
            new_page()
        ops.append(("heading", y, title, sub))
        y += 9
        for i in range(0, len(items), COLS):
            if y + CELL_H > BOTTOM:
                new_page()
                ops.append(("heading", y, title + " (continued)", ""))
                y += 9
            ops.append(("cells", y, items[i:i + COLS]))
            y += CELL_H
        y += 6

    new_page()
    ops.append(("heading", y, "Sample sentences", "Write each one on the line(s) below it, in your normal handwriting"))
    y += 11
    for text in SENTENCES:
        n = 1 if len(text) <= 34 else 2
        need = 5 + LINE_H * n + 2.5
        if y + need > BOTTOM:
            new_page()
        ops.append(("sentence", y, text, n))
        y += need
    return pages


def _draw_cell(c, x, y, ch, name):
    c.setStrokeColorRGB(*GREY); c.setLineWidth(0.6); c.setDash()
    c.rect(x * mm, (PH - y - CELL_H) * mm, CELL_W * mm, CELL_H * mm)
    base = PH - y - CELL_H + 7                                   # baseline 7 mm above the box bottom
    c.setLineWidth(0.5); c.setStrokeColorRGB(*GUIDE); c.setDash()
    c.line((x + 1.5) * mm, base * mm, (x + CELL_W - 1.5) * mm, base * mm)
    c.setDash(1.2, 1.8); c.setStrokeColorRGB(*LIGHT)
    for off in (5.5, 10.5):                                          # x-height and cap-height guides
        c.line((x + 1.5) * mm, (base + off) * mm, (x + CELL_W - 1.5) * mm, (base + off) * mm)
    c.setDash()
    c.setFillColorRGB(0.45, 0.45, 0.52); c.setFont("Helvetica", 9)
    c.drawString((x + 1.6) * mm, (PH - y - 4.2) * mm, ch)
    if name:
        c.setFont("Helvetica", 5); c.drawRightString((x + CELL_W - 1.4) * mm, (PH - y - CELL_H + 1.5) * mm, name)


def _draw_page(c, ops, n, total):
    c.setFillColorRGB(0, 0, 0)
    for cx, cy in ((7, 7), (PW - 11, 7), (7, PH - 11), (PW - 11, PH - 11)):   # registration marks
        c.rect(cx * mm, (PH - cy - 4) * mm, 4 * mm, 4 * mm, stroke=0, fill=1)
    c.setFillColorRGB(*GREY); c.setFont("Helvetica", 7)
    c.drawString(LEFT * mm, 9.5 * mm, "Inkify AI \u00b7 My Handwriting Template")
    c.drawRightString(RIGHT * mm, 9.5 * mm, f"INKIFY-HW-{TEMPLATE_VERSION} \u00b7 Page {n} of {total}")
    for op in ops:
        kind, y = op[0], op[1]
        if kind == "title":
            c.setFillColorRGB(*PURPLE); c.setFont("Helvetica-Bold", 20)
            c.drawString(LEFT * mm, (PH - y - 7) * mm, "My Handwriting Template")
            c.setFillColorRGB(*GREY); c.setFont("Helvetica", 9)
            c.drawString(LEFT * mm, (PH - y - 13) * mm, "Fill in every box, then scan or photograph the pages and upload them to Inkify AI.")
        elif kind == "instructions":
            h = op[2]
            c.setFillColorRGB(0.957, 0.945, 1); c.setStrokeColorRGB(0.89, 0.87, 0.98); c.setLineWidth(0.8)
            c.roundRect(LEFT * mm, (PH - y - h) * mm, (RIGHT - LEFT) * mm, h * mm, 3 * mm, stroke=1, fill=1)
            c.setFillColorRGB(*PURPLE); c.setFont("Helvetica-Bold", 9.5)
            c.drawString((LEFT + 5) * mm, (PH - y - 6.5) * mm, "How to fill in this template")
            c.setFillColorRGB(0.11, 0.1, 0.18); c.setFont("Helvetica", 9)
            yy = y + 11.5
            for k, t in enumerate(INSTRUCTIONS, 1):
                for j, ln in enumerate(simpleSplit(t, "Helvetica", 9, (RIGHT - LEFT - 10) * mm)):
                    c.drawString((LEFT + 5) * mm, (PH - yy) * mm, (f"{k}.  " if j == 0 else "      ") + ln)
                    yy += 4.6
        elif kind == "heading":
            c.setFillColorRGB(*PURPLE); c.setFont("Helvetica-Bold", 11)
            c.drawString(LEFT * mm, (PH - y - 5.5) * mm, op[2])
            if op[3]:
                w = c.stringWidth(op[2], "Helvetica-Bold", 11)
                c.setFillColorRGB(*GREY); c.setFont("Helvetica", 8.5)
                c.drawString(LEFT * mm + w + 3 * mm, (PH - y - 5.5) * mm, op[3])
        elif kind == "cells":
            for i, (ch, name) in enumerate(op[2]):
                _draw_cell(c, LEFT + i * CELL_W, y, ch, name)
        elif kind == "sentence":
            text, lines = op[2], op[3]
            c.setFillColorRGB(*GREY); c.setFont("Helvetica", 8)
            c.drawString(LEFT * mm, (PH - y - 3.5) * mm, text)
            top = y + 5
            for k in range(lines):
                ly = top + k * LINE_H
                c.setStrokeColorRGB(*GREY); c.setLineWidth(0.5); c.setDash()
                c.rect(LEFT * mm, (PH - ly - LINE_H) * mm, (RIGHT - LEFT) * mm, LINE_H * mm)
                c.setStrokeColorRGB(*GUIDE); c.setLineWidth(0.5)
                c.line((LEFT + 1.5) * mm, (PH - ly - LINE_H + 4) * mm, (RIGHT - 1.5) * mm, (PH - ly - LINE_H + 4) * mm)
    c.showPage()


@lru_cache(maxsize=1)
def build_template_pdf():
    """Returns the finished template PDF as bytes (built once per process)."""
    pages = _layout()
    buf = BytesIO()
    c = canvas.Canvas(buf, pagesize=A4, pageCompression=1)
    c.setTitle("Inkify AI - My Handwriting Template")
    c.setAuthor("Inkify AI")
    c.setSubject("Printable handwriting sample sheet")
    for i, ops in enumerate(pages, 1):
        _draw_page(c, ops, i, len(pages))
    c.save()
    return buf.getvalue()
