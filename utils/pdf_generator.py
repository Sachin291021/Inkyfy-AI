"""Builds the downloadable PDF from the rendered page images.

Why not Pillow's ``save(..., save_all=True)``?  It needs every page in memory at once, re-encodes each page as JPEG
(larger and lossy) and gives us no control over the file structure.  Here every page is written straight to disk as
it is produced (lossless Flate-compressed RGB image + a one-line content stream), so memory stays flat for any
page count, and the result is a plain, standards-compliant PDF 1.4 file with an exact cross-reference table.

Safety rules:
  * the PDF is written to ``<name>.tmp``, flushed + fsync'ed, fully verified, and only then renamed into place
    (``os.replace`` is atomic), so a half-written file can never be served;
  * ``validate_pdf`` really parses the structure (xref offsets, object headers, stream lengths, page count) and,
    in deep mode, decompresses every page image - a truncated file with a fake ``%%EOF`` is rejected.
"""
import os
import re
import secrets
import zlib
from pathlib import Path

from .handwriting_engine import DPI


class PdfError(Exception):
    """Raised when a PDF could not be built or failed verification."""


# --------------------------------------------------------------------------------------------- writer
class PdfWriter:
    """Streaming multi-page PDF writer: ``with PdfWriter(tmp) as w: w.add_page(img) ...; w.pages``."""

    def __init__(self, path, dpi=DPI):
        self.path, self.dpi, self.pages = Path(path), dpi, 0
        self._offsets = {}           # object number -> byte offset
        self._page_objs = []         # object numbers of the /Page dictionaries
        self._next = 3               # 1 = Catalog, 2 = Pages (both written last)
        self._f = open(self.path, "wb")
        self._pos = 0
        self._write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")   # 2nd line = binary marker, so tools treat the file as binary

    def _write(self, data):
        self._f.write(data); self._pos += len(data)

    def _obj(self, num, body, stream=None):
        self._offsets[num] = self._pos
        out = b"%d 0 obj\n" % num + body
        if stream is not None:
            out += b"\nstream\n" + stream + b"\nendstream"
        self._write(out + b"\nendobj\n")

    def add_page(self, im):
        if self._f is None: raise PdfError("PDF writer is closed")
        im = im.convert("RGB")
        w, h = im.size
        if w < 1 or h < 1: raise PdfError("Cannot add an empty page image")
        data = zlib.compress(im.tobytes(), 6)
        img, cnt, page = self._next, self._next + 1, self._next + 2
        self._next += 3
        mw, mh = w * 72.0 / self.dpi, h * 72.0 / self.dpi            # page size in PDF points
        self._obj(img, (b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
                        b"/BitsPerComponent 8 /Filter /FlateDecode /Length %d >>" % (w, h, len(data))), data)
        content = b"q %.4f 0 0 %.4f 0 0 cm /Im0 Do Q" % (mw, mh)
        self._obj(cnt, b"<< /Length %d >>" % len(content), content)
        self._obj(page, (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %.4f %.4f] "
                         b"/Resources << /XObject << /Im0 %d 0 R >> /ProcSet [/PDF /ImageC] >> "
                         b"/Contents %d 0 R >>" % (mw, mh, img, cnt)))
        self._page_objs.append(page)
        self.pages += 1

    def close(self):
        """Write the page tree, catalog, xref table and trailer, then flush to disk."""
        if self._f is None: return
        try:
            if not self._page_objs: raise PdfError("A PDF needs at least one page")
            kids = b" ".join(b"%d 0 R" % n for n in self._page_objs)
            self._obj(2, b"<< /Type /Pages /Count %d /Kids [%s] >>" % (len(self._page_objs), kids))
            self._obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
            size = self._next
            xref_pos = self._pos
            rows = [b"0000000000 65535 f \n"] + [b"%010d 00000 n \n" % self._offsets[n] for n in range(1, size)]
            ident = secrets.token_hex(16).encode()
            self._write(b"xref\n0 %d\n" % size + b"".join(rows))     # every row is exactly 20 bytes
            self._write(b"trailer\n<< /Size %d /Root 1 0 R /ID [<%s> <%s>] >>\nstartxref\n%d\n%%%%EOF\n"
                        % (size, ident, ident, xref_pos))
            self._f.flush(); os.fsync(self._f.fileno())
        finally:
            self._f.close(); self._f = None

    def abort(self):
        """Close the file without finishing it (the caller deletes the temp file)."""
        if self._f is not None:
            try: self._f.close()
            except Exception: pass
            self._f = None

    def __enter__(self): return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None: self.close()
        elif self._f is not None:
            try: self._f.close()
            except Exception: pass
            self._f = None
        return False


# ------------------------------------------------------------------------------------------- validator
_STARTXREF = re.compile(rb"startxref\s+(\d+)\s+%%EOF\s*$")


def validate_pdf(path, expected_pages=None, deep=False):
    """Structurally verify a PDF produced by PdfWriter. Returns the page count; raises PdfError otherwise.

    deep=True also inflates every page image and checks it has exactly width*height*3 bytes (use after writing)."""
    try:
        data = Path(path).read_bytes()
    except OSError as e:
        raise PdfError(f"cannot read file: {e}")
    n = len(data)
    if n < 200 or not data.startswith(b"%PDF-1."): raise PdfError("missing %PDF- header")
    m = _STARTXREF.search(data[-256:])
    if not m: raise PdfError("missing startxref / %%EOF at end of file (file is truncated)")
    xpos = int(m.group(1))
    if not (0 < xpos < n) or not data.startswith(b"xref\n", xpos): raise PdfError("startxref does not point to an xref table")
    hdr = re.match(rb"xref\n0 (\d+)\n", data[xpos:xpos + 32])
    if not hdr: raise PdfError("malformed xref header")
    size = int(hdr.group(1)); rows = xpos + hdr.end()
    tpos = rows + 20 * size
    if size < 4 or not data.startswith(b"trailer\n", tpos): raise PdfError("xref table is damaged")
    trailer = data[tpos:]
    ms, mr = re.search(rb"/Size (\d+)", trailer), re.search(rb"/Root (\d+) 0 R", trailer)
    if not ms or int(ms.group(1)) != size or not mr: raise PdfError("bad trailer")
    offs = {}
    for i in range(1, size):
        row = data[rows + 20 * i: rows + 20 * i + 20]
        if len(row) != 20 or not re.fullmatch(rb"\d{10} 00000 n \n", row): raise PdfError(f"bad xref entry {i}")
        off = int(row[:10])
        if not data.startswith(b"%d 0 obj\n" % i, off): raise PdfError(f"object {i} not at its xref offset")
        offs[i] = off

    def obj_body(i):
        start = offs[i]; end = data.find(b"\nendobj\n", start)
        if end < 0 or end > xpos: raise PdfError(f"object {i} is not terminated")
        return data[start:end]

    pages_n = 0
    for i in offs:
        body = obj_body(i)
        is_img = b"/Subtype /Image" in body
        if is_img or re.search(rb"^\d+ 0 obj\n<< /Length \d+ >>\nstream\n", body):
            ml = re.search(rb"/Length (\d+)", body); sp = body.find(b"\nstream\n")
            if not ml or sp < 0: raise PdfError(f"object {i}: bad stream")
            ln = int(ml.group(1)); s0 = sp + len(b"\nstream\n")
            if body[s0 + ln:] != b"\nendstream": raise PdfError(f"object {i}: stream length mismatch (truncated)")
            if deep and is_img:
                mw, mh = re.search(rb"/Width (\d+)", body), re.search(rb"/Height (\d+)", body)
                try: raw = zlib.decompress(body[s0:s0 + ln])
                except zlib.error as e: raise PdfError(f"object {i}: image data is corrupt ({e})")
                if len(raw) != int(mw.group(1)) * int(mh.group(1)) * 3: raise PdfError(f"object {i}: image has wrong size")
        if b"/Type /Page " in body: pages_n += 1
    if int(mr.group(1)) not in offs: raise PdfError("catalog missing")
    mc = re.search(rb"/Type /Pages /Count (\d+)", obj_body(2))
    if not mc or int(mc.group(1)) != pages_n or pages_n < 1: raise PdfError("page tree does not match the pages in the file")
    if expected_pages is not None and pages_n != expected_pages:
        raise PdfError(f"expected {expected_pages} pages but the file has {pages_n}")
    return pages_n


def is_valid_pdf(path, expected_pages=None, deep=False):
    """Boolean wrapper used by the download route."""
    try:
        validate_pdf(path, expected_pages, deep); return True
    except PdfError:
        return False


# ------------------------------------------------------------------------------------ atomic convenience
def save_pdf(pages, path):
    """Write an iterable of PIL images to ``path`` atomically. Returns the page count; raises on any problem."""
    path = Path(path); tmp = path.with_name(path.name + ".tmp")
    try:
        with PdfWriter(tmp) as w:
            for im in pages: w.add_page(im)
        validate_pdf(tmp, w.pages, deep=True)
        os.replace(tmp, path)
        return w.pages
    finally:
        tmp.unlink(missing_ok=True)
