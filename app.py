"""Inkify AI - Word to Handwriting Converter (Flask backend)."""
import json, logging, os, re, shutil, tempfile, time, uuid
from pathlib import Path
from flask import Flask, Response, jsonify, render_template, request, send_file, url_for
from werkzeug.exceptions import HTTPException
from werkzeug.utils import secure_filename
from utils.document_reader import blocks_to_text, text_to_blocks
from utils.extractors import LABEL_BY_KIND, ExtractionError, check_upload, extract
from utils.handwriting_engine import HIGHLIGHT_MODES, PAPERS, SIZES_MM, STYLES, THICKNESS, iter_pages
from utils.pens import PALETTE_ORDER, PEN_COLORS, ROLE_DEFAULTS, parse_pen, strip_tags, to_hex, used_pens
from utils.pdf_generator import PdfError, PdfWriter, validate_pdf
from own_handwriting import create_blueprint as create_handwriting_blueprint

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
BASE = Path(__file__).resolve().parent


def _storage_root():
    """Folder for uploads/outputs: $DATA_DIR if set, else the project folder; if that is read-only (some hosts mount
    the code read-only) fall back to the system temp dir. All gunicorn workers on one instance share it."""
    for root in (Path(os.environ["DATA_DIR"]) if os.environ.get("DATA_DIR") else BASE, Path(tempfile.gettempdir()) / "inkify"):
        try:
            for sub in ("uploads", "outputs"): (root / sub).mkdir(parents=True, exist_ok=True)
            probe = root / "outputs" / f".w{os.getpid()}"; probe.write_bytes(b"ok"); probe.unlink()
            return root
        except OSError:
            continue
    raise RuntimeError("No writable storage folder available")


STORAGE = _storage_root()
UPLOADS, OUTPUTS = STORAGE / "uploads", STORAGE / "outputs"

app = Flask(__name__)
try:  # gzip/brotli for HTML, CSS, JS, JSON and SVG (big Lighthouse win); the app still runs if the package is missing
    from flask_compress import Compress
    app.config["COMPRESS_MIN_SIZE"] = 500
    Compress(app)
except ImportError:
    pass


def _asset_version(rel):
    try: return str(int((BASE / "static" / rel).stat().st_mtime))
    except OSError: return "0"


@app.context_processor
def _asset_helper():
    """asset('dist/app.min.css') -> /static/dist/app.min.css?v=<mtime>  (cache-busting URL, safe to cache for a year)."""
    return {"asset": lambda rel: url_for("static", filename=rel, v=_asset_version(rel)), "inline_css": _inline_css}


_css_cache = {}


def _inline_css():
    """The whole stylesheet is inlined in the HTML (no render-blocking request). Re-read only when the file changes."""
    f = BASE / "static" / "dist" / "app.min.css"
    key = _asset_version("dist/app.min.css")
    if _css_cache.get("k") != key:
        _css_cache["k"], _css_cache["v"] = key, f.read_text(encoding="utf-8").replace("../webfonts/", "/static/webfonts/")
    return _css_cache["v"]


@app.after_request
def _cache_headers(resp):
    """Long cache for static files (fonts/CSS/JS are versioned or immutable); short revalidation for the HTML page."""
    if request.path.startswith("/static/") and resp.status_code == 200:
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif request.path == "/" and resp.status_code == 200:
        resp.headers["Cache-Control"] = "public, max-age=0, must-revalidate"
    return resp
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10 MB upload limit
app.register_blueprint(create_handwriting_blueprint(STORAGE))  # "My Own Handwriting": template, sample upload, profiles (/api/hw/*)
ID_RE = re.compile(r"^[a-f0-9]{32}$")
TTL = int(os.environ.get("FILE_TTL_HOURS", 6)) * 3600  # generated files are deleted after N hours of inactivity
CLEAN_EVERY = 300                                      # run the cleanup at most every 5 minutes
_last_clean = 0.0
log = logging.getLogger("inkify")


def err(msg, code=400):
    r = jsonify(error=msg); r.status_code = code
    r.headers["Cache-Control"] = "no-store"
    return r


def _newest_mtime(p):
    """Last activity of a document folder = newest mtime of the folder or anything inside it.
    (A folder's own mtime does NOT change when a file inside is rewritten or downloaded.)"""
    try:
        latest = p.stat().st_mtime
        for f in p.iterdir():
            try: latest = max(latest, f.stat().st_mtime)
            except OSError: pass
        return latest
    except OSError:
        return time.time()  # vanished / unreadable -> treat as fresh, skip it


def cleanup(force=False):
    """Purge folders idle for longer than TTL. Throttled, race-safe between workers, and it never
    touches a folder that was used recently (so an in-progress download is never deleted)."""
    global _last_clean
    now = time.time()
    if not force and now - _last_clean < CLEAN_EVERY: return
    _last_clean = now
    for root in (UPLOADS, OUTPUTS):
        try: entries = list(root.iterdir())
        except OSError: continue
        for p in entries:
            if p.name == ".gitkeep": continue
            try:
                if now - (_newest_mtime(p) if p.is_dir() else p.stat().st_mtime) > TTL:
                    shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
            except OSError:
                pass  # another worker got there first


def touch(d):
    """Mark a document folder as in use so cleanup leaves it alone."""
    try: os.utime(d, None)
    except OSError: pass


def num(v, lo, hi, default):
    try: return max(lo, min(hi, float(v)))
    except (TypeError, ValueError): return default


def clean_options(o):
    o = o if isinstance(o, dict) else {}
    pick = lambda k, ok, d: o.get(k) if o.get(k) in ok else d
    raw = o.get("pens") if isinstance(o.get("pens"), dict) else {}
    pens = {role: to_hex(parse_pen(raw.get(role)) or PEN_COLORS[default]) for role, default in ROLE_DEFAULTS.items()}
    heading = parse_pen(raw.get("heading"))                 # no heading pen chosen -> headings follow the body pen
    pens["heading"] = to_hex(heading) if heading else "body"
    return {"style": pick("style", STYLES, "student"),
            "ink": to_hex(parse_pen(o.get("ink")) or PEN_COLORS["blue"]),   # body pen: preset name, #hex or rgb(...)
            "pens": pens,                                                    # pens for headings / important / definition / highlight
            "thickness": pick("thickness", THICKNESS, "medium"), "highlight": pick("highlight", HIGHLIGHT_MODES, "off"),
            "ink_intensity": num(o.get("ink_intensity"), 40, 100, 100), "ink_variation": num(o.get("ink_variation"), 0, 10, 3),
            "paper": pick("paper", PAPERS, "ruled"), "page_size": pick("page_size", SIZES_MM, "A4"),
            "font_size": num(o.get("font_size"), 12, 32, 18), "line_spacing": num(o.get("line_spacing"), 1.0, 2.5, 1.5),
            "margin": num(o.get("margin"), 8, 40, 15), "left_margin": num(o.get("left_margin"), 0, 40, 10),
            "irregularity": num(o.get("irregularity"), 0, 10, 3)}


def doc_dir(doc_id):
    return OUTPUTS / doc_id if ID_RE.match(doc_id or "") and (OUTPUTS / doc_id).is_dir() else None


@app.before_request
def _purge(): cleanup()


@app.errorhandler(413)
def too_big(_):
    if request.path.startswith("/api/hw/"): return err("This file is too large (max 8 MB per handwriting sample).", 413)
    return err("File is too large (max 10 MB).", 413)


@app.errorhandler(HTTPException)
def http_error(e):
    """Always answer API calls with JSON (never an HTML error page the front-end can't parse)."""
    if request.path.startswith("/api/"):
        return err({404: "Not found.", 405: "Method not allowed."}.get(e.code, e.description or "Request failed."), e.code or 500)
    return e


@app.errorhandler(Exception)
def server_error(e):
    log.exception("Unhandled error")
    return err("Something went wrong on the server. Please try again.", 500)


@app.route("/")
def index():
    return render_template("index.html", pens=[(n, to_hex(PEN_COLORS[n])) for n in PALETTE_ORDER])

@app.route("/health")
def health():
    return {"status": "ok"}, 200

@app.route("/robots.txt")
def robots():
    base = request.url_root.rstrip("/")
    body = f"User-agent: *\\nAllow: /\\nDisallow: /api/\\n\\nSitemap: {base}/sitemap.xml\\n"
    return Response(body, mimetype="text/plain")

@app.route("/sitemap.xml")
def sitemap():
    base = request.url_root.rstrip("/")
    urls = ["/"]
    xml = '<?xml version="1.0" encoding="UTF-8"?>\\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\\n'
    for path in urls:
        xml += f"  <url><loc>{base}{path}</loc></url>\\n"
    xml += "</urlset>"
    return Response(xml, mimetype="application/xml")



@app.post("/api/upload")
def upload():
    """Accepts .docx, .pdf, .png, .jpg, .jpeg. Whatever the format, the text ends up as the same blocks the DOCX reader
    produces, so editing, live preview and PDF rendering are shared by all of them."""
    f = request.files.get("file")
    if not f or not f.filename: return err("No file received.")
    safe = secure_filename(f.filename)
    ext = os.path.splitext(safe)[1].lower()
    try:
        kind = check_upload(ext, f.mimetype)                       # extension + declared MIME type
    except ExtractionError as e:
        return err(str(e), e.status)
    doc_id = uuid.uuid4().hex
    tmp = UPLOADS / f"{doc_id}{ext}"  # generated name; client filename never touches the disk
    try:
        f.save(tmp)
        if tmp.stat().st_size == 0: return err("This file is empty.", 422)
        result = extract(tmp, kind)                                # sniffs the real format, then reads / OCRs
    except ExtractionError as e:
        return err(str(e), e.status)
    except Exception:
        log.exception("Upload processing failed (kind=%s)", kind)
        return err("Could not read this file. It may be corrupted.", 422)
    finally:
        tmp.unlink(missing_ok=True)  # original upload is removed immediately
    blocks = result.blocks
    words = sum(len(strip_tags(b["text"]).split()) for b in blocks)
    if not words: return err("This document has no text to convert.", 422)
    d = OUTPUTS / doc_id; d.mkdir()
    (d / "doc.json").write_text(json.dumps(blocks))
    (d / "name.txt").write_text(Path(safe).stem[:60] or "document")
    (d / "orig.json").write_text(json.dumps(blocks))  # kept so edits can be reset
    out = dict(id=doc_id, words=words, paragraphs=sum(1 for b in blocks if b["text"]), type=LABEL_BY_KIND[kind])
    if result.pages: out["pages"] = result.pages
    if result.ocr: out["ocr"] = True
    if result.notices: out["notice"] = " ".join(result.notices)
    return jsonify(out)


@app.get("/api/doc/<doc_id>")
def get_doc(doc_id):
    """Returns the current (or, with ?original=1, the as-uploaded) text for the editor."""
    d = doc_dir(doc_id)
    if not d: return err("Session expired. Please upload your document again.", 404)
    try:
        blocks = json.loads((d / ("orig.json" if request.args.get("original") else "doc.json")).read_text())
    except (OSError, ValueError):
        return err("Session expired. Please upload your document again.", 404)
    return jsonify(text=blocks_to_text(blocks))


@app.post("/api/doc")
def save_doc():
    """Saves user edits. The old PDF no longer matches the text, so it is no longer the "latest"."""
    data = request.get_json(silent=True) or {}
    d = doc_dir(data.get("id"))
    if not d: return err("Session expired. Please upload your document again.", 404)
    text = data.get("text")
    if not isinstance(text, str) or len(text) > 200_000: return err("Invalid or too long text.")
    blocks = text_to_blocks(text)
    words = sum(len(strip_tags(b["text"]).split()) for b in blocks)
    if not words: return err("The text can't be empty.", 422)
    (d / "doc.json").write_text(json.dumps(blocks))
    (d / "latest.txt").unlink(missing_ok=True)   # existing PDFs stay until cleanup, so a running download is never cut off
    touch(d)
    return jsonify(words=words)


def _save_png(im, dest):
    """Preview pages are written atomically too, so /api/page can never serve a half-written image."""
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        im.save(tmp, "PNG", compress_level=1); os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


@app.post("/api/render")
def render():
    data = request.get_json(silent=True) or {}
    d = doc_dir(data.get("id"))
    if not d: return err("Session expired. Please upload your document again.", 404)
    try:
        blocks = json.loads((d / "doc.json").read_text())
    except (OSError, ValueError):
        return err("Session expired. Please upload your document again.", 404)
    raw_options = data.get("options") if isinstance(data.get("options"), dict) else {}
    if raw_options.get("style") == "custom":
        return err("Your custom handwriting profile can be selected, but its character renderer is not yet connected to PDF generation. Choose a standard style for now.", 422)
    opts, final = clean_options(raw_options), bool(data.get("final"))
    pdf_id = uuid.uuid4().hex if final else None          # one unique, never-overwritten file per conversion
    tmp_pdf = d / f"{pdf_id}.pdf.tmp" if final else None
    writer, n, stage = None, 0, "render"
    try:
        if final: writer = PdfWriter(tmp_pdf)
        for im in iter_pages(blocks, opts):               # pages stream through one at a time (flat memory)
            n += 1
            stage = "render"; _save_png(im, d / f"p{n}.png")
            if writer: stage = "pdf"; writer.add_page(im)
        if writer:
            stage = "pdf"; writer.close(); writer = None
            validate_pdf(tmp_pdf, n, deep=True)           # inflate + check every page BEFORE it becomes downloadable
            os.replace(tmp_pdf, d / f"{pdf_id}.pdf")      # atomic: the .pdf name only ever points at a complete file
    except ValueError as e:                               # e.g. "Document is too long"
        return err(str(e), 422)
    except Exception as e:
        log.exception("Render failed (stage=%s, final=%s, doc=%s)", stage, final, d.name)
        return err("Could not create the PDF. Please try again or use different settings." if stage == "pdf" or isinstance(e, PdfError)
                   else "Rendering failed. Please try different settings.", 500)
    finally:
        if writer: writer.abort()
        if tmp_pdf: tmp_pdf.unlink(missing_ok=True)
    for old in d.glob("p*.png"):                          # drop preview pages left over from a longer earlier render
        try:
            if int(old.stem[1:]) > n: old.unlink(missing_ok=True)
        except ValueError: pass
    out = {"pages": n, "pens": used_pens(blocks, opts)}   # colours really used -> legend under the preview
    if final:
        for old in d.glob("*.pdf"):                       # drop PDFs from earlier conversions (not the new one)
            try:
                if old.stem != pdf_id and time.time() - old.stat().st_mtime > 600: old.unlink(missing_ok=True)
            except OSError: pass
        (d / "latest.txt").write_text(pdf_id)
        out.update(download=f"/api/download/{d.name}/{pdf_id}", filename=_pdf_name(d))
        log.info("PDF ready doc=%s pages=%d bytes=%d", d.name, n, (d / f"{pdf_id}.pdf").stat().st_size)
    touch(d)
    return jsonify(out)


def _pdf_name(d):
    try: stem = (d / "name.txt").read_text().strip()
    except OSError: stem = ""
    return (stem or "document") + "-handwritten.pdf"


@app.get("/api/page/<doc_id>/<int:n>")
def page(doc_id, n):
    d = doc_dir(doc_id)
    if not d or not (d / f"p{n}.png").exists(): return err("Page not found.", 404)
    r = send_file(d / f"p{n}.png", mimetype="image/png"); r.headers["Cache-Control"] = "no-store"; return r


def _send_pdf(doc_id, pdf_id):
    d = doc_dir(doc_id)
    if not d or not ID_RE.match(pdf_id or ""):                 # blocks "../", slashes, anything but 32 hex chars
        return err("File not found. Please convert the document again.", 404)
    f = (d / f"{pdf_id}.pdf").resolve()
    if f.parent != d.resolve() or not f.is_file():              # defence in depth against path traversal
        return err("This PDF is no longer available. Please convert the document again.", 404)
    try:
        validate_pdf(f)                                          # cheap structural check (header, xref, EOF, page tree)
    except PdfError as e:
        log.error("Refusing to serve damaged PDF %s: %s", f.name, e)
        f.unlink(missing_ok=True)                                # it can never become valid again
        return err("The generated PDF is damaged. Please convert the document again.", 500)
    touch(d)
    r = send_file(f, mimetype="application/pdf", as_attachment=True, download_name=_pdf_name(d), max_age=0, conditional=True)
    r.headers["Cache-Control"] = "no-store"
    r.headers["X-Content-Type-Options"] = "nosniff"
    return r


@app.get("/api/download/<doc_id>/<pdf_id>")
def download(doc_id, pdf_id):
    return _send_pdf(doc_id, pdf_id)


@app.get("/api/download/<doc_id>")
def download_latest(doc_id):
    """Backwards-compatible link: serves the most recent conversion."""
    d = doc_dir(doc_id)
    try: pdf_id = (d / "latest.txt").read_text().strip() if d else ""
    except OSError: pdf_id = ""
    return _send_pdf(doc_id, pdf_id)


if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1", port=int(os.environ.get("PORT", 5000)))
