"""Text extraction for PDF and image uploads.

Everything here ends in the same "blocks" structure that utils.document_reader.read_docx() produces
({"text": str, "level": 0-3}; empty text = blank line), so the existing editor, live preview and handwriting
renderer are reused unchanged.

  PDF   : text layer via pypdfium2 (already a dependency) -> lines -> paragraphs/headings.
          Pages without a text layer (scans) are rendered and sent through OCR.
  Image : Pillow pre-processing (grayscale, contrast, background flattening, denoise, Otsu threshold) -> Tesseract OCR.

OCR needs the Tesseract binary on the server (`apt install tesseract-ocr`) plus the `pytesseract` wrapper.
If it is missing, PDFs that have real text still work and scans/images get a clear error instead of a crash.
"""
import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from statistics import median

from PIL import Image, ImageChops, ImageFilter, ImageOps, ImageStat

from .document_reader import read_docx

log = logging.getLogger("inkify.extract")
os.environ.setdefault("OMP_THREAD_LIMIT", "1")       # Tesseract: one thread per OCR call (several gunicorn workers share the CPU)

# ---- limits -----------------------------------------------------------------------------------------------------
MAX_PDF_PAGES = 100            # pages we are willing to read text from
MAX_OCR_PAGES = 15             # scanned pages we OCR per upload (OCR is slow; the rest are reported, not silently dropped)
MAX_IMAGE_PIXELS = 50_000_000  # decompression-bomb guard
MIN_IMAGE_SIDE = 24            # px - anything smaller cannot contain readable text
MAX_TEXT_CHARS = 200_000       # same cap /api/doc applies to edited text
OCR_TIMEOUT = int(os.environ.get("OCR_TIMEOUT", 60))   # seconds per OCR call
OCR_LANG = os.environ.get("OCR_LANG", "eng")           # e.g. "eng+hin" if those traineddata files are installed
TEXT_LAYER_MIN_CHARS = 15      # fewer letters/digits than this on a PDF page => treat the page as a scan

# ---- accepted types ---------------------------------------------------------------------------------------------
KIND_BY_EXT = {".docx": "docx", ".pdf": "pdf", ".png": "image", ".jpg": "image", ".jpeg": "image"}
LABEL_BY_KIND = {"docx": "Word", "pdf": "PDF", "image": "Image"}
_GENERIC_MIME = {"", "application/octet-stream", "binary/octet-stream", "application/force-download"}
MIME_BY_EXT = {
    ".pdf": {"application/pdf", "application/x-pdf", "application/acrobat", "text/pdf"},
    ".png": {"image/png", "image/x-png"},
    ".jpg": {"image/jpeg", "image/jpg", "image/pjpeg"},
    ".jpeg": {"image/jpeg", "image/jpg", "image/pjpeg"},
}
UNSUPPORTED_MSG = "Unsupported file type. Please upload a .docx, .pdf, .png, .jpg or .jpeg file."


class ExtractionError(Exception):
    """A problem the user can understand; `status` is the HTTP status the API answers with."""
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


@dataclass
class Extraction:
    blocks: list
    kind: str                                     # "docx" | "pdf" | "image"
    pages: int = 0
    ocr: bool = False                             # True if any text came from OCR
    notices: list = field(default_factory=list)   # non-fatal remarks shown to the user


# ---- validation -------------------------------------------------------------------------------------------------
def check_upload(ext, mimetype):
    """Extension + declared MIME check (before anything is saved). Returns the kind, raises ExtractionError.
    The declared MIME type is client-supplied, so it only rejects *clearly wrong* types; the real check is sniff()."""
    kind = KIND_BY_EXT.get(ext)
    if not kind:
        raise ExtractionError(UNSUPPORTED_MSG, 400)
    mime = (mimetype or "").split(";")[0].strip().lower()
    if kind != "docx" and mime not in _GENERIC_MIME and mime not in MIME_BY_EXT[ext]:
        raise ExtractionError(f"This file's type ({mime}) doesn't match its {ext} extension.", 400)
    return kind                                   # .docx keeps its previous behaviour: the content check in read_docx decides


def sniff(path):
    """Detects the real format from magic bytes: 'pdf' | 'png' | 'jpeg' | 'zip' | None."""
    with open(path, "rb") as f:
        head = f.read(1024)
    if b"%PDF-" in head: return "pdf"
    if head.startswith(b"\x89PNG\r\n\x1a\n"): return "png"
    if head.startswith(b"\xff\xd8\xff"): return "jpeg"
    if head.startswith(b"PK"): return "zip"
    return None


# ---- shared text helpers ----------------------------------------------------------------------------------------
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\ufffe\uffff\u00ad]")
_ALNUM = re.compile(r"[^\W_]", re.UNICODE)
_TERMINAL = re.compile(r"""[.!?:;…]["')\]”’]*$""")


def _alnum_count(s):
    return len(_ALNUM.findall(s))


def _clean_line(s):
    s = _CTRL.sub("", s).replace("\u00a0", " ").replace("\t", " ")
    s = " ".join(s.split())
    return re.sub(r"^#{1,3}\s", "", s)            # a leading "# " would turn into a heading when the user edits the text


def _join_lines(lines):
    """Joins wrapped lines into one paragraph; re-joins words split by a line-end hyphen (exam- / ple)."""
    out = ""
    for ln in lines:
        if not out: out = ln
        elif re.search(r"[A-Za-z]{2}-$", out) and ln[:1].islower(): out = out[:-1] + ln
        else: out += " " + ln
    return out


_BULLET = re.compile(r"^([•▪◦●○■□‣\-–—*]|\(?\d{1,3}[.)]|\(?[a-zA-Z][.)])\s+")


def _blocks_from_paragraphs(paras):
    """paras: [(text, level)] -> blocks with a blank line between paragraphs (same shape read_docx produces).
    Consecutive list items ("1. ...", "- ...") are kept together without blank lines."""
    blocks, prev_item = [], False
    for text, level in paras:
        if not text.strip(): continue
        item = level == 0 and bool(_BULLET.match(text))
        if blocks and not (item and prev_item): blocks.append({"text": "", "level": 0})
        blocks.append({"text": text, "level": level}); prev_item = item
    return blocks


def _text_to_paragraphs(raw):
    """OCR output -> [(text, 0)]. Tesseract separates paragraphs with blank lines and wraps with single newlines."""
    paras = []
    for chunk in re.split(r"\n\s*\n", raw.replace("\r", "")):
        lines = [_clean_line(l) for l in chunk.split("\n")]
        lines = [l for l in lines if _alnum_count(l) >= 1]       # drop stray "|", "—", ")" etc. that OCR invents from smudges
        if lines: paras.append((_join_lines(lines), 0))
    return paras


# ---- OCR --------------------------------------------------------------------------------------------------------
_ocr_state = {}


def ocr_status():
    """(available, reason). Cached; checks both the Python wrapper and the tesseract binary."""
    if "ok" not in _ocr_state:
        try:
            import pytesseract
            if not shutil.which(pytesseract.pytesseract.tesseract_cmd):
                raise FileNotFoundError("tesseract binary not found")
            pytesseract.get_tesseract_version()
            _ocr_state.update(ok=True, why="")
        except Exception as e:
            log.warning("OCR unavailable: %s", e)
            _ocr_state.update(ok=False, why=str(e))
    return _ocr_state["ok"], _ocr_state["why"]


def _require_ocr():
    if not ocr_status()[0]:
        raise ExtractionError("Text recognition (OCR) isn't available on this server, so scanned PDFs and images "
                              "can't be read right now. Please upload a .docx or a PDF with selectable text.", 503)


def _flatten_to_rgb(im):
    """Any PIL mode -> RGB, transparency composited onto white (a transparent PNG would otherwise turn black)."""
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255)); bg.alpha_composite(im)
        return bg.convert("RGB")
    if im.mode in ("I;16", "I;16B", "I;16L", "I"):
        im = im.point(lambda p: p / 256).convert("L")
    return im.convert("RGB")


def _otsu(hist):
    """Otsu's threshold from a 256-bin histogram (pure Python - no numpy/OpenCV needed)."""
    total = sum(hist)
    if not total: return 128
    sum_all = sum(i * h for i, h in enumerate(hist))
    w_b = sum_b = 0; best, thr = -1.0, 128
    for i, h in enumerate(hist):
        w_b += h
        if not w_b: continue
        w_f = total - w_b
        if not w_f: break
        sum_b += i * h
        m_b, m_f = sum_b / w_b, (sum_all - sum_b) / w_f
        var = w_b * w_f * (m_b - m_f) ** 2
        if var > best: best, thr = var, i
    return thr


def _flatten_background(g):
    """Evens out uneven lighting (phone photos, shadows): estimate the paper colour with a blurred, text-free copy,
    then lift the paper to white while keeping ink dark."""
    w, h = g.size
    small = g.resize((max(1, w // 8), max(1, h // 8)), Image.BILINEAR)
    small = small.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(5))   # max filter wipes thin dark strokes
    bg = small.resize((w, h), Image.BILINEAR)
    return ImageChops.add(g, ImageChops.invert(bg))


def preprocess(im):
    """Returns (grayscale, black-and-white) versions of the image, tuned for OCR:
    grayscale -> normalise contrast -> (invert light-on-dark) -> scale to a good size -> flatten lighting ->
    median denoise -> Otsu threshold."""
    g = ImageOps.grayscale(_flatten_to_rgb(im))
    w, h = g.size
    long_side = max(w, h)
    if long_side < 1800:                                  # tesseract likes ~30px tall letters: enlarge small images
        f = min(3.0, 1800 / long_side); g = g.resize((round(w * f), round(h * f)), Image.LANCZOS)
    elif long_side > 4200:                                # huge photos only slow OCR down
        f = 4200 / long_side; g = g.resize((round(w * f), round(h * f)), Image.LANCZOS)
    g = ImageOps.autocontrast(g, cutoff=1)
    if ImageStat.Stat(g).mean[0] < 110:                   # light text on a dark background -> dark on light
        g = ImageOps.invert(g)
    g = _flatten_background(g)
    g = g.filter(ImageFilter.MedianFilter(3))             # salt-and-pepper / JPEG noise
    t = _otsu(g.histogram())
    bw = g.point(lambda p: 255 if p > t else 0)
    return g, bw


MIN_WORD_CONF = 40         # drop individual words Tesseract itself is unsure about (smudges, noise, stray marks)
MIN_SOLID_CONF = 70        # a word recognised this confidently is "solid"
MIN_SOLID_RATIO = 0.25     # real text is mostly solid words; noise/textures produce many words of which very few are solid


def _ocr_pass(img, psm):
    """One Tesseract run -> (paragraph lines, score, solid words, all words). Per-word confidence discards noise.
    score = summed confidence of the *solid* words only, so hallucinated junk can't win when variants are compared."""
    import pytesseract
    d = pytesseract.image_to_data(img, lang=OCR_LANG, config=f"--psm {psm}", timeout=OCR_TIMEOUT,
                                  output_type=pytesseract.Output.DICT)
    paras, score, solid, total = {}, 0.0, 0, 0
    for i, word in enumerate(d["text"]):
        word = (word or "").strip()
        try: conf = float(d["conf"][i])
        except (TypeError, ValueError): continue
        if word and conf >= 0: total += 1
        if not word or conf < MIN_WORD_CONF: continue
        if not _alnum_count(word) and len(word) > 1: continue          # runs of punctuation are never words
        key = (d["block_num"][i], d["par_num"][i]); line = d["line_num"][i]
        paras.setdefault(key, {}).setdefault(line, []).append(word)
        if conf >= MIN_SOLID_CONF and _alnum_count(word) >= 2: solid += 1; score += conf
    out = [[" ".join(ws) for _, ws in sorted(lines.items())] for _, lines in sorted(paras.items())]
    return out, score, solid, total


def ocr_image(im):
    """OCR for one PIL image -> [(text, level)]. Tries the cleaned black-and-white version first and only falls back to
    other variants when it finds little text. Raises ExtractionError on engine failure; returns [] if no text."""
    _require_ocr()
    import pytesseract
    gray, bw = preprocess(im)
    best, best_score, best_solid, best_total = [], 0.0, 0, 0
    for img, psm in ((bw, 3), (gray, 3), (bw, 6)):
        try:
            paras, score, solid, total = _ocr_pass(img, psm)
        except RuntimeError as e:                         # pytesseract raises RuntimeError("Tesseract process timeout")
            log.warning("OCR timeout/failure: %s", e)
            raise ExtractionError("Text recognition took too long. Try a smaller or clearer image.", 422)
        except pytesseract.TesseractError as e:
            log.warning("tesseract error: %s", e)
            raise ExtractionError("Text recognition failed (is the language data installed?). Please try another file.", 500)
        except Exception:
            log.exception("OCR crashed")
            raise ExtractionError("Text recognition failed. Please try another file.", 500)
        if score > best_score: best, best_score, best_solid, best_total = paras, score, solid, total
        if best_solid >= 20: break                        # plenty of confident text: skip the slower fallbacks
    if best_solid < 1 or best_solid < best_total * MIN_SOLID_RATIO:
        return []                                         # nothing Tesseract is confident about / mostly noise = no real text
    out = []
    for lines in best:
        lines = [_clean_line(l) for l in lines]
        lines = [l for l in lines if _alnum_count(l) >= 1]
        if lines: out.append((_join_lines(lines), 0))
    return out


# ---- images -----------------------------------------------------------------------------------------------------
def extract_image(path):
    try:
        with Image.open(path) as probe:
            w, h = probe.size
            probe.verify()                                # cheap integrity check (needs a re-open afterwards)
    except Exception:
        raise ExtractionError("This image is corrupted or not a valid PNG/JPG file.", 422)
    if w * h > MAX_IMAGE_PIXELS:
        raise ExtractionError("This image is too large to process. Please use one under 50 megapixels.", 422)
    if min(w, h) < MIN_IMAGE_SIDE:
        raise ExtractionError("This image is too small to contain readable text.", 422)
    try:
        with Image.open(path) as im:
            im.load()
            im = ImageOps.exif_transpose(im)              # phone photos: honour the rotation flag
            paras = ocr_image(im)
    except ExtractionError:
        raise
    except Exception as e:
        log.warning("Image decode failed: %s", e)
        raise ExtractionError("This image is corrupted or not a valid PNG/JPG file.", 422)
    if not paras:
        raise ExtractionError("No text could be detected in this image. Use a clear, well-lit picture of printed text.", 422)
    return Extraction(_blocks_from_paragraphs(paras), "image", pages=1, ocr=True)


# ---- PDF --------------------------------------------------------------------------------------------------------
def _page_paragraphs(page):
    """Text layer of one PDF page -> [{"text", "level", "open"}] using line geometry to rebuild paragraphs and headings.
    "open" = the paragraph doesn't end a sentence (it may continue on the next page)."""
    tp = page.get_textpage()
    try:
        text = tp.get_text_range()
        lines, pos = [], 0
        for m in list(re.finditer(r"\r\n|\r|\n", text)) + [None]:
            end = m.start() if m else len(text)
            seg = text[pos:end]
            idx = next((i for i, ch in enumerate(seg) if not ch.isspace()), None)
            clean = _clean_line(seg)
            if idx is not None and clean:
                try:
                    l, b, _r, t = tp.get_charbox(pos + idx, loose=True)     # font-metric box: identical for 'C', 'o' and 'p'
                    last = len(seg.rstrip()) - 1
                    r = tp.get_charbox(pos + last, loose=True)[2]
                    lines.append({"t": clean, "x": l, "r": r, "y": b, "h": max(t - b, 0.1)})
                except Exception:                           # a glyph without a box: keep the text, lose only its geometry
                    lines.append({"t": clean, "x": None, "r": None, "y": None, "h": None})
            if m: pos = m.end()
    finally:
        tp.close()
    if not lines: return []

    geo = [ln for ln in lines if ln["y"] is not None]
    gaps = [a["y"] - b["y"] for a, b in zip(geo, geo[1:]) if 0 < a["y"] - b["y"] < 4 * a["h"]]
    pitch = median(gaps) if gaps else None
    body_h = median([ln["h"] for ln in geo]) if geo else None
    max_r = max((ln["r"] for ln in geo), default=0); min_x = min((ln["x"] for ln in geo), default=0)
    width = max(max_r - min_x, 1)

    def level(ln):                                          # headings: clearly larger type than the page's body text
        if ln["h"] is None or not body_h or len(ln["t"]) > 120: return 0
        ratio = ln["h"] / body_h
        return 1 if ratio >= 1.7 else 2 if ratio >= 1.3 else 0

    paras, cur, prev = [], None, None
    for ln in lines:
        lv = level(ln); new = cur is None
        if cur is not None and prev is not None:
            gap = (prev["y"] - ln["y"]) if (prev["y"] is not None and ln["y"] is not None) else None
            new = (lv != cur["level"]                                       # heading <-> body, or different heading size
                   or (lv and gap is not None and pitch and gap > pitch * 1.6)
                   or (not lv and gap is not None and pitch and gap > pitch * 1.45)       # blank-line gap
                   or (not lv and bool(_BULLET.match(ln["t"])))                             # list item
                   or (not lv and ln["x"] is not None and prev["x"] is not None and ln["x"] - prev["x"] > body_h * 1.2
                       and bool(_TERMINAL.search(prev["t"])))                              # first-line indent after a full stop
                   or (not lv and prev["r"] is not None and prev["r"] < min_x + width * 0.72
                       and bool(_TERMINAL.search(prev["t"]))))                             # short last line of a paragraph
        if new:
            cur = {"lines": [], "level": lv}; paras.append(cur)
        cur["lines"].append(ln["t"]); prev = ln
    out = []
    for p in paras:
        t = _join_lines(p["lines"])
        out.append({"text": t, "level": p["level"], "open": p["level"] == 0 and not _TERMINAL.search(t)})
    return out


def extract_pdf(path):
    try:
        import pypdfium2 as pdfium
    except Exception:
        raise ExtractionError("PDF support isn't installed on this server.", 503)
    try:
        pdf = pdfium.PdfDocument(str(path))
    except Exception as e:
        if "password" in str(e).lower():
            raise ExtractionError("This PDF is password-protected. Remove the password and upload it again.", 422)
        raise ExtractionError("This PDF is corrupted or not a valid PDF file.", 422)
    try:
        n = len(pdf)
        if n == 0: raise ExtractionError("This PDF has no pages.", 422)
        if n > MAX_PDF_PAGES:
            raise ExtractionError(f"This PDF has {n} pages (max {MAX_PDF_PAGES}). Please split it and upload a shorter part.", 422)
        merged, ocr_done, ocr_skipped, ocr_failed = [], 0, 0, False
        ocr_ok = ocr_status()[0]
        for i in range(n):
            page = pdf[i]
            try:
                try:
                    paras = _page_paragraphs(page)
                except Exception:
                    log.exception("Text layer unreadable on page %d", i + 1); paras = []
                if sum(_alnum_count(p["text"]) for p in paras) < TEXT_LAYER_MIN_CHARS:     # no text layer -> scanned page
                    if ocr_done >= MAX_OCR_PAGES or not ocr_ok:
                        ocr_skipped += 1; continue
                    scale = max(1.0, min(300 / 72, 4200 / max(page.get_size())))               # ~300 dpi, capped for huge pages
                    try:
                        pil = page.render(scale=scale).to_pil()
                    except Exception:
                        log.exception("Could not render page %d", i + 1); ocr_failed = True; continue
                    try:
                        paras = [{"text": t, "level": lv, "open": not _TERMINAL.search(t)} for t, lv in ocr_image(pil)]
                    finally:
                        pil.close()
                    ocr_done += 1
                for p in paras:                                         # a paragraph cut by a page break continues on the next page
                    if (merged and merged[-1]["open"] and merged[-1]["level"] == 0 and p["level"] == 0 and p["text"][:1].islower()):
                        merged[-1]["text"] = _join_lines([merged[-1]["text"], p["text"]]); merged[-1]["open"] = p["open"]
                    else:
                        merged.append(dict(p))
            finally:
                page.close()
    finally:
        pdf.close()

    notices = []
    if ocr_skipped:
        if not ocr_ok:
            notices.append(f"{ocr_skipped} scanned page(s) were skipped because text recognition isn't available on this server.")
        else:
            notices.append(f"Only the first {MAX_OCR_PAGES} scanned pages were read; {ocr_skipped} more page(s) were skipped.")
    if ocr_failed:
        notices.append("Some pages could not be read.")
    if not merged:
        if ocr_skipped and not ocr_ok:
            _require_ocr()                                              # raises the "OCR unavailable" error
        raise ExtractionError("No text could be found in this PDF. It may be blank or contain only pictures.", 422)
    return Extraction(_blocks_from_paragraphs([(p["text"], p["level"]) for p in merged]), "pdf", pages=n,
                      ocr=ocr_done > 0, notices=notices)


# ---- entry point ------------------------------------------------------------------------------------------------
def extract(path, kind):
    """Dispatches on the validated kind. The file's real format (magic bytes) must agree with its extension."""
    real = sniff(path)
    if kind == "docx":
        if real != "zip": raise ExtractionError("This file is not a valid Word (.docx) document.", 422)
        try:
            return Extraction(read_docx(path), "docx")
        except ValueError as e:
            raise ExtractionError(str(e), 422)
        except Exception:
            raise ExtractionError("Could not read this document. It may be corrupted.", 422)
    if kind == "pdf":
        if real != "pdf": raise ExtractionError("This file is not a valid PDF (it may be corrupted or renamed).", 422)
        result = extract_pdf(path)
    elif kind == "image":
        if real not in ("png", "jpeg"): raise ExtractionError("This file is not a valid PNG or JPG image.", 422)
        result = extract_image(path)
    else:
        raise ExtractionError(UNSUPPORTED_MSG, 400)
    if sum(len(b["text"]) for b in result.blocks) > MAX_TEXT_CHARS:
        raise ExtractionError("This file contains too much text to convert (max about 200,000 characters).", 422)
    return result
