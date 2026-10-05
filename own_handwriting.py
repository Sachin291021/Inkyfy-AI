"""\"My Own Handwriting\" - Flask blueprint (Part 1: template download, sample upload, profile management).

Registered from app.py with ``app.register_blueprint(create_blueprint(STORAGE))``. All routes live under ``/api/hw``.

Privacy model: there are no user accounts, so each browser gets a random, unguessable owner id in an HttpOnly cookie
(``hw_owner``). Profiles and samples are stored per owner and every route checks it - uploaded files have no public URL,
they are only served to the browser that uploaded them.

Part 2 hooks: ``store`` (utils.profiles.ProfileStore) exposes ``samples()`` / ``sample_path()``; ``process_profile`` below is the
route Part 2 will implement (currently answers 501 "coming soon").
"""
import logging
import uuid
import json
import io
from pathlib import Path
from PIL import Image, ImageOps, ImageFilter
from io import BytesIO

from flask import Blueprint, g, jsonify, request, send_file

from utils import profiles as P
from utils.profiles import ID_RE, ProfileError, ProfileStore
from utils.template_pdf import TEMPLATE_VERSION, build_template_pdf

log = logging.getLogger("inkify.hw")
COOKIE = "hw_owner"
COOKIE_AGE = 365 * 24 * 3600


def _json(payload, status=200):
    r = jsonify(payload)
    r.status_code = status
    r.headers["Cache-Control"] = "no-store"
    return r


def create_blueprint(storage_root):
    bp = Blueprint("own_handwriting", __name__, url_prefix="/api/hw")
    store = ProfileStore(storage_root)
    bp.store = store                      # Part 2: ``from own_handwriting import ...`` or app.blueprints["own_handwriting"].store

    # ------------------------------------------------------------------ owner cookie
    @bp.before_request
    def _owner():
        cookie = request.cookies.get(COOKIE, "")
        g.new_owner = not ID_RE.match(cookie)
        g.owner = uuid.uuid4().hex if g.new_owner else cookie

    @bp.after_request
    def _set_cookie(resp):
        if getattr(g, "new_owner", False):
            resp.set_cookie(COOKIE, g.owner, max_age=COOKIE_AGE, httponly=True, samesite="Lax", secure=request.is_secure, path="/")
        return resp

    @bp.errorhandler(ProfileError)
    def _profile_error(e):
        return _json({"error": e.message, "code": e.code}, e.status)

    # ------------------------------------------------------------------ helpers
    def decorate(profile):
        base = f"/api/hw/profiles/{profile['id']}/samples"
        for s in profile.get("samples", []):
            s["thumb"], s["url"] = f"{base}/{s['id']}/thumb", f"{base}/{s['id']}/file"
        return profile

    def body():
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------ public info + template
    @bp.get("/limits")
    def limits():
        return _json({"max_sample_bytes": P.MAX_SAMPLE_BYTES, "max_samples": P.MAX_SAMPLES, "name_max": P.NAME_MAX,
                      "extensions": sorted(P.ALLOWED_EXT), "min_side": P.MIN_SIDE, "max_pdf_pages": P.MAX_PDF_PAGES})

    @bp.get("/template.pdf")
    def template():
        pdf = build_template_pdf()
        inline = request.args.get("inline") == "1"           # "Print" opens it in the browser's PDF viewer
        r = send_file(BytesIO(pdf), mimetype="application/pdf", as_attachment=not inline,
                      download_name=f"Inkify-Handwriting-Template-{TEMPLATE_VERSION}.pdf", max_age=3600)
        r.headers["X-Content-Type-Options"] = "nosniff"
        return r

    # ------------------------------------------------------------------ profiles
    @bp.get("/profiles")
    def list_profiles():
        store.sweep()
        out = store.list_saved(g.owner) if not g.new_owner else []
        for p in out:
            for s in p["samples"]:
                s["thumb"] = f"/api/hw/profiles/{p['id']}/samples/{s['id']}/thumb"
        return _json({"profiles": out})

    @bp.post("/profiles")
    def create_profile():
        return _json(decorate(store.create(g.owner, body().get("name"))), 201)

    @bp.get("/profiles/<pid>")
    def get_profile(pid):
        return _json(decorate(store.get(g.owner, pid)))

    @bp.post("/profiles/<pid>/save")
    def save_profile(pid):
        return _json(decorate(store.save(g.owner, pid, body().get("name"))))

    @bp.delete("/profiles/<pid>")
    def delete_profile(pid):
        store.delete(g.owner, pid)
        return _json({"ok": True})

    # ------------------------------------------------------------------ samples
    @bp.post("/profiles/<pid>/samples")
    def upload_sample(pid):
        f = request.files.get("file")
        if not f or not f.filename:
            raise ProfileError("No file received.", 400, "no_file")
        data = f.read(P.MAX_SAMPLE_BYTES + 1)                # never read more than the limit into memory
        meta = store.add_sample(g.owner, pid, f.filename, data)
        log.info("sample added profile=%s kind=%s bytes=%d", pid, meta["kind"], meta["bytes"])
        base = f"/api/hw/profiles/{pid}/samples/{meta['id']}"
        return _json({**meta, "thumb": f"{base}/thumb", "url": f"{base}/file"}, 201)

    @bp.delete("/profiles/<pid>/samples/<sid>")
    def delete_sample(pid, sid):
        store.delete_sample(g.owner, pid, sid)
        return _json({"ok": True})

    @bp.get("/profiles/<pid>/samples/<sid>/thumb")
    def sample_thumb(pid, sid):
        r = send_file(store.thumb_path(g.owner, pid, sid), mimetype="image/jpeg", max_age=0)
        r.headers["Cache-Control"] = "private, max-age=600"
        r.headers["X-Content-Type-Options"] = "nosniff"
        return r

    @bp.get("/profiles/<pid>/samples/<sid>/file")
    def sample_file(pid, sid):
        path = store.sample_path(g.owner, pid, sid)
        mime = {".png": "image/png", ".jpg": "image/jpeg", ".pdf": "application/pdf"}[path.suffix]   # from OUR stored type, never the client's
        r = send_file(path, mimetype=mime, as_attachment=False, max_age=0)
        r.headers["Cache-Control"] = "private, no-store"
        r.headers["X-Content-Type-Options"] = "nosniff"
        r.headers["Content-Security-Policy"] = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"
        return r

    # ------------------------------------------------------------------ handwriting analysis
    @bp.post("/profiles/<pid>/process")
    def process_profile(pid):
        """Analyze each sample and persist normalized ink/line assets for reuse.

        This deliberately does not claim to synthesize a true font: it creates a reusable
        normalized handwriting texture dataset and descriptive processing metadata.
        """
        profile = store.get(g.owner, pid)  # validates ownership and profile id
        samples = store.samples(g.owner, pid)
        if not samples:
            raise ProfileError("Upload at least one handwriting sample before processing.", 422, "no_samples")
        pdir = store._pdir(g.owner, pid)
        work = pdir / "processed"
        work.mkdir(exist_ok=True)
        assets_dir = work / "assets"
        assets_dir.mkdir(exist_ok=True)
        assets, page_count, warnings = [], 0, []
        try:
            import pypdfium2 as pdfium
            for sample in samples:
                path = store.sample_path(g.owner, pid, sample["id"])
                pages = []
                if path.suffix.lower() == ".pdf":
                    doc = pdfium.PdfDocument(str(path))
                    try:
                        for page in doc:
                            pages.append(page.render(scale=2).to_pil().convert("RGB"))
                    finally:
                        doc.close()
                else:
                    with Image.open(path) as im:
                        pages.append(ImageOps.exif_transpose(im).convert("RGB"))
                for page_index, image in enumerate(pages):
                    page_count += 1
                    gray = ImageOps.grayscale(image)
                    # Normalize the writing mask; remove paper background and tiny scanner noise.
                    gray = ImageOps.autocontrast(gray)
                    mask = gray.point(lambda px: 255 if px < 185 else 0)
                    mask = mask.filter(ImageFilter.MedianFilter(3))
                    bbox = mask.getbbox()
                    if not bbox:
                        warnings.append(f"{sample['name']}: no visible ink detected on page {page_index + 1}")
                        continue
                    cropped = mask.crop(bbox)
                    # Keep source aspect ratio and cap asset dimensions for predictable storage.
                    cropped.thumbnail((1800, 2400))
                    asset_name = f"{sample['id']}-{page_index + 1}.png"
                    tmp = assets_dir / (asset_name + ".tmp")
                    cropped.save(tmp, format="PNG", optimize=True)
                    tmp.replace(assets_dir / asset_name)
                    assets.append({"file": asset_name, "sample_id": sample["id"], "page": page_index + 1,
                                   "width": cropped.width, "height": cropped.height,
                                   "ink_coverage": round(sum(1 for v in cropped.getdata() if v) / (cropped.width * cropped.height), 4)})
            if not assets:
                raise ProfileError("No handwriting could be detected. Upload a clear, well-lit page with dark writing on a plain background.", 422, "no_handwriting")
            result = {"version": 1, "method": "normalized_ink_texture", "created": __import__("time").time(),
                      "pages_processed": page_count, "assets": assets, "warnings": warnings,
                      "note": "Samples are analyzed and saved as reusable handwriting assets. Character-level font synthesis is not available for this profile yet."}
            (work / "dataset.json.tmp").write_text(json.dumps(result), encoding="utf-8")
            (work / "dataset.json.tmp").replace(work / "dataset.json")
            meta_path = pdir / "profile.json"
            meta = json.loads(meta_path.read_text("utf-8"))
            meta["processing"] = {"state": "ready", "engine": "normalized_ink_texture_v1", "updated": result["created"],
                                  "asset_count": len(assets), "pages_processed": page_count, "warnings": warnings,
                                  "note": result["note"]}
            meta["updated"] = result["created"]
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
            return _json({"ok": True, "processing": meta["processing"], "message": "Handwriting samples processed and saved.", "warnings": warnings})
        except ProfileError:
            raise
        except Exception as exc:
            log.exception("Handwriting processing failed for profile %s", pid)
            meta_path = pdir / "profile.json"
            try:
                meta = json.loads(meta_path.read_text("utf-8"))
                meta["processing"] = {"state": "failed", "engine": None, "updated": __import__("time").time(), "error": "Sample analysis failed. Please check the files and try again."}
                meta_path.write_text(json.dumps(meta), encoding="utf-8")
            except Exception:
                pass
            raise ProfileError("Sample analysis failed. Please upload clear PNG, JPG or PDF handwriting samples and try again.", 422, "processing_failed") from exc

    return bp
