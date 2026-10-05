"""Storage + validation for "My Own Handwriting" profiles (Part 1: samples only, no handwriting analysis yet).

Disk layout (everything is addressed by generated 32-hex ids; client file names never touch the disk)::

    <STORAGE>/profiles/<owner_id>/<profile_id>/
        profile.json              name, created, saved flag, processing state (hook for Part 2)
        samples/<sample_id>.<ext> the validated original upload (png | jpg | pdf)
        samples/<sample_id>.json  per-sample metadata (one file per sample => parallel uploads cannot clobber each other)
        thumbs/<sample_id>.jpg    preview image (first page for PDFs)

``owner_id`` is a random id kept in an HttpOnly cookie, so one visitor can never list or open another visitor's profiles.

Part 2 entry points: ``ProfileStore.get`` / ``ProfileStore.samples`` / ``ProfileStore.sample_path`` (original files) and the
``processing`` block in profile.json (``state`` = not_started | queued | processing | ready | failed).
"""
import io
import json
import os
import re
import shutil
import time
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

try:                                    # PDF validation + page-1 thumbnails (optional but recommended)
    import pypdfium2 as pdfium
except Exception:                       # pragma: no cover - falls back to a structural check
    pdfium = None

ID_RE = re.compile(r"^[a-f0-9]{32}$")
ALLOWED_EXT = {"png", "jpg", "jpeg", "pdf"}
MAX_SAMPLE_BYTES = 8 * 1024 * 1024      # per file (stays below the app-wide 10 MB request cap, multipart overhead included)
MAX_SAMPLES = 20                        # per profile
MAX_PROFILE_BYTES = 60 * 1024 * 1024    # total originals per profile
MAX_PROFILES = 30                       # saved profiles per visitor
MAX_PIXELS = 50_000_000                 # decompression-bomb guard
MIN_SIDE = 200                          # px - smaller images cannot hold readable handwriting
MAX_PDF_PAGES = 12
THUMB = 480
DRAFT_TTL = 24 * 3600                   # unsaved profiles are removed after a day of inactivity
NAME_MAX = 40


class ProfileError(Exception):
    """An error whose message is safe and clear enough to show to the user."""

    def __init__(self, message, status=400, code="invalid"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def _now():
    return time.time()


def _write_atomic(path, data):
    tmp = path.with_name(path.name + f".{uuid.uuid4().hex[:6]}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_json(path):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None


def clean_name(raw):
    """Profile name: trimmed, single-spaced, no control characters, 1-40 chars."""
    name = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", str(raw or ""))).strip()
    if not name:
        raise ProfileError("Please enter a profile name.", 422, "name_required")
    if len(name) > NAME_MAX:
        raise ProfileError(f"Profile name is too long (max {NAME_MAX} characters).", 422, "name_too_long")
    return name


def display_filename(raw):
    """Name shown in the UI only (the real file is stored under a generated id). Unicode is fine; paths/control chars are not."""
    name = re.split(r"[\\/]", str(raw or ""))[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return (name[:80] or "sample")


# ------------------------------------------------------------------------------------------ sample validation
def detect_kind(data):
    """Real type from the file's own bytes (the extension is never trusted)."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if b"%PDF-" in data[:1024]:
        return "pdf"
    return None


def _placeholder_thumb(label="PDF"):
    im = Image.new("RGB", (360, 480), (244, 241, 255))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((90, 110, 270, 370), 18, outline=(91, 43, 214), width=6, fill="white")
    try:
        font = ImageFont.load_default(size=64)
    except TypeError:                   # very old Pillow
        font = ImageFont.load_default()
    d.text((180, 240), label, fill=(91, 43, 214), anchor="mm", font=font)
    return im


def _validate_image(data):
    """Returns (thumbnail, meta). Rejects corrupted / truncated / oversized / tiny images."""
    try:
        with Image.open(io.BytesIO(data)) as im:
            w, h = im.size
            if im.format not in ("PNG", "JPEG"):
                raise ProfileError("Unsupported image format. Please use PNG, JPG or JPEG.", 415, "unsupported")
            if w * h > MAX_PIXELS:
                raise ProfileError("This image has too many pixels. Please upload a smaller scan or photo (max 50 megapixels).", 422, "too_large_dimensions")
            im.load()                    # full decode: catches truncated / corrupted data that verify() would miss
            if min(w, h) < MIN_SIDE:
                raise ProfileError(f"This image is too small ({w}×{h}px). Use at least {MIN_SIDE}px on the shorter side.", 422, "too_small")
            im = ImageOps.exif_transpose(im)
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, "white")
                bg.paste(im, mask=im.split()[-1])
                im = bg
            else:
                im = im.convert("RGB")
            thumb = im.copy()
            thumb.thumbnail((THUMB, THUMB))
            return thumb, {"width": w, "height": h}
    except ProfileError:
        raise
    except Exception:                    # PIL raises many different types for broken files
        raise ProfileError("This image appears to be corrupted and could not be read. Please re-scan or re-save it and try again.", 422, "corrupted")


def _validate_pdf(data):
    """Returns (thumbnail, meta). Rejects broken, password-protected and over-long PDFs."""
    if pdfium is None:                  # structural fallback
        if b"%%EOF" not in data[-2048:]:
            raise ProfileError("This PDF appears to be corrupted or incomplete.", 422, "corrupted")
        return _placeholder_thumb(), {"pages": None}
    try:
        pdf = pdfium.PdfDocument(data)
    except Exception as e:
        if "password" in str(e).lower():
            raise ProfileError("This PDF is password-protected. Please remove the password and upload it again.", 422, "encrypted")
        raise ProfileError("This PDF appears to be corrupted and could not be opened.", 422, "corrupted")
    try:
        pages = len(pdf)
        if pages < 1:
            raise ProfileError("This PDF has no pages.", 422, "empty")
        if pages > MAX_PDF_PAGES:
            raise ProfileError(f"This PDF has {pages} pages (max {MAX_PDF_PAGES}). Please upload only the pages you filled in.", 422, "too_many_pages")
        page = pdf[0]
        w_pt, h_pt = page.get_size()
        scale = min(THUMB / max(w_pt, 1), THUMB / max(h_pt, 1), 3)
        thumb = page.render(scale=scale).to_pil().convert("RGB")
        return thumb, {"pages": pages}
    except ProfileError:
        raise
    except Exception:
        raise ProfileError("This PDF appears to be corrupted and could not be read.", 422, "corrupted")
    finally:
        pdf.close()


def validate_sample(filename, data):
    """Full validation of one upload. Returns (kind, thumbnail_image, meta) or raises ProfileError."""
    ext = (filename or "").rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    if ext not in ALLOWED_EXT:
        raise ProfileError("Unsupported file type. Please upload a PNG, JPG, JPEG or PDF file.", 415, "unsupported")
    if not data:
        raise ProfileError("This file is empty (0 bytes).", 422, "empty")
    if len(data) > MAX_SAMPLE_BYTES:
        raise ProfileError(f"This file is too large (max {MAX_SAMPLE_BYTES // 1048576} MB per sample).", 413, "too_large")
    kind = detect_kind(data)
    if kind is None:
        raise ProfileError("This file is not a real PNG, JPG or PDF (its contents don't match its extension).", 415, "unsupported")
    thumb, meta = _validate_pdf(data) if kind == "pdf" else _validate_image(data)
    return kind, thumb, meta


# ------------------------------------------------------------------------------------------------- the store
class ProfileStore:
    def __init__(self, storage_root):
        self.root = Path(storage_root) / "profiles"
        self.root.mkdir(parents=True, exist_ok=True)
        self._last_sweep = 0.0

    # ---- paths (ids are validated, so nothing user-controlled can escape the profiles folder) ----
    def _owner_dir(self, owner):
        if not ID_RE.match(owner or ""):
            raise ProfileError("Profile not found.", 404, "not_found")
        return self.root / owner

    def _pdir(self, owner, pid, must_exist=True):
        if not ID_RE.match(pid or ""):
            raise ProfileError("Profile not found.", 404, "not_found")
        d = self._owner_dir(owner) / pid
        if must_exist and not (d / "profile.json").is_file():
            raise ProfileError("Profile not found. It may have been deleted or expired.", 404, "not_found")
        return d

    # ---- profiles ----
    def _load(self, d):
        meta = _read_json(d / "profile.json")
        if not isinstance(meta, dict):
            raise ProfileError("Profile not found.", 404, "not_found")
        return meta

    def _public(self, meta, d):
        samples = self.samples_from_dir(d)
        return {"id": meta["id"], "name": meta["name"], "created": meta["created"], "updated": meta.get("updated", meta["created"]),
                "saved": bool(meta.get("saved")), "sample_count": len(samples), "samples": samples,
                "processing": meta.get("processing", {"state": "not_started"})}

    def create(self, owner, name=None):
        self.sweep()
        od = self._owner_dir(owner)
        od.mkdir(parents=True, exist_ok=True)
        pid = uuid.uuid4().hex           # (the per-visitor profile cap is enforced when saving)
        d = od / pid
        (d / "samples").mkdir(parents=True)
        (d / "thumbs").mkdir()
        t = _now()
        meta = {"id": pid, "schema": 1, "name": clean_name(name) if name and str(name).strip() else "Untitled profile",
                "created": t, "updated": t, "saved": False,
                "processing": {"state": "not_started", "engine": None, "updated": None}}   # Part 2 fills this in
        _write_atomic(d / "profile.json", json.dumps(meta).encode())
        return self._public(meta, d)

    def _iter_meta(self, owner):
        try:
            dirs = list(self._owner_dir(owner).iterdir())
        except (OSError, ProfileError):
            return
        for d in dirs:
            if d.is_dir() and ID_RE.match(d.name):
                m = _read_json(d / "profile.json")
                if isinstance(m, dict) and "id" in m:
                    yield m

    def list_saved(self, owner):
        out = []
        for m in self._iter_meta(owner):
            if m.get("saved"):
                out.append(self._public(m, self.root / owner / m["id"]))
        for p in out:                    # list view doesn't need the sample details
            p["samples"] = [{"id": s["id"], "name": s["name"]} for s in p["samples"]][:4]
        return sorted(out, key=lambda p: p["created"], reverse=True)

    def get(self, owner, pid):
        d = self._pdir(owner, pid)
        return self._public(self._load(d), d)

    def save(self, owner, pid, name):
        d = self._pdir(owner, pid)
        meta = self._load(d)
        name = clean_name(name)
        if not self.samples_from_dir(d):
            raise ProfileError("Upload at least one handwriting sample before saving.", 422, "no_samples")
        others = [m for m in self._iter_meta(owner) if m.get("saved") and m["id"] != pid]
        if any(m["name"].casefold() == name.casefold() for m in others):
            raise ProfileError(f"You already have a profile named “{name}”. Please choose a different name.", 409, "name_taken")
        if not meta.get("saved") and len(others) >= MAX_PROFILES:
            raise ProfileError(f"You can keep up to {MAX_PROFILES} profiles. Delete one to save a new profile.", 409, "too_many_profiles")
        meta.update(name=name, saved=True, updated=_now())
        _write_atomic(d / "profile.json", json.dumps(meta).encode())
        return self._public(meta, d)

    def delete(self, owner, pid):
        d = self._pdir(owner, pid)
        shutil.rmtree(d, ignore_errors=True)

    # ---- samples ----
    def samples_from_dir(self, d):
        out = []
        for f in (d / "samples").glob("*.json"):
            m = _read_json(f)
            if isinstance(m, dict) and ID_RE.match(str(m.get("id", ""))) and self._sample_file(d, m["id"]):
                out.append(m)
        return sorted(out, key=lambda m: m.get("created", 0))

    def samples(self, owner, pid):
        """Part 2: list of sample metadata dicts for a profile."""
        return self.samples_from_dir(self._pdir(owner, pid))

    def _sample_file(self, d, sid):
        for ext in ("png", "jpg", "pdf"):
            f = d / "samples" / f"{sid}.{ext}"
            if f.is_file():
                return f
        return None

    def sample_path(self, owner, pid, sid):
        """Part 2: absolute path of the original sample file (validated ids only)."""
        d = self._pdir(owner, pid)
        f = self._sample_file(d, sid) if ID_RE.match(sid or "") else None
        if not f:
            raise ProfileError("Sample not found.", 404, "not_found")
        return f

    def thumb_path(self, owner, pid, sid):
        d = self._pdir(owner, pid)
        f = d / "thumbs" / f"{sid}.jpg"
        if not ID_RE.match(sid or "") or not f.is_file():
            raise ProfileError("Preview not found.", 404, "not_found")
        return f

    def add_sample(self, owner, pid, filename, data):
        d = self._pdir(owner, pid)
        existing = self.samples_from_dir(d)
        if len(existing) >= MAX_SAMPLES:
            raise ProfileError(f"You can add up to {MAX_SAMPLES} samples per profile.", 409, "too_many_samples")
        if sum(m.get("bytes", 0) for m in existing) + len(data) > MAX_PROFILE_BYTES:
            raise ProfileError("This profile has reached its total upload limit. Remove a sample to add another.", 409, "profile_full")
        kind, thumb, extra = validate_sample(filename, data)
        sid = uuid.uuid4().hex
        buf = io.BytesIO()
        thumb.save(buf, "JPEG", quality=82)
        _write_atomic(d / "thumbs" / f"{sid}.jpg", buf.getvalue())
        _write_atomic(d / "samples" / f"{sid}.{kind}", data)
        meta = {"id": sid, "name": display_filename(filename), "kind": kind, "bytes": len(data), "created": _now(), **extra}
        _write_atomic(d / "samples" / f"{sid}.json", json.dumps(meta).encode())   # written last: a sample "exists" only when complete
        self._touch(d)
        return meta

    def delete_sample(self, owner, pid, sid):
        d = self._pdir(owner, pid)
        if not ID_RE.match(sid or "") or not self._sample_file(d, sid):
            raise ProfileError("Sample not found.", 404, "not_found")
        (d / "samples" / f"{sid}.json").unlink(missing_ok=True)       # metadata first => it vanishes from listings at once
        for ext in ("png", "jpg", "pdf"):
            (d / "samples" / f"{sid}.{ext}").unlink(missing_ok=True)
        (d / "thumbs" / f"{sid}.jpg").unlink(missing_ok=True)
        self._touch(d)

    def _touch(self, d):
        meta = _read_json(d / "profile.json")
        if isinstance(meta, dict):
            meta["updated"] = _now()
            _write_atomic(d / "profile.json", json.dumps(meta).encode())

    # ---- housekeeping ----
    def sweep(self, force=False):
        """Removes UNSAVED profiles idle for more than DRAFT_TTL (saved profiles are never auto-deleted). Throttled."""
        now = _now()
        if not force and now - self._last_sweep < 1800:
            return
        self._last_sweep = now
        try:
            owners = [o for o in self.root.iterdir() if o.is_dir()]
        except OSError:
            return
        for od in owners:
            try:
                for d in od.iterdir():
                    m = _read_json(d / "profile.json") if d.is_dir() else None
                    if d.is_dir() and (m is None or not m.get("saved")) and now - (m or {}).get("updated", 0) > DRAFT_TTL:
                        if m is None and now - d.stat().st_mtime < DRAFT_TTL:
                            continue     # half-created folder that is still fresh
                        shutil.rmtree(d, ignore_errors=True)
                if not any(od.iterdir()):
                    od.rmdir()
            except OSError:
                pass
