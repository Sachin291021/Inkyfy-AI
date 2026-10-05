# Inkify AI – Word to Handwriting Converter

Upload a `.docx`, **PDF or image** (`.pdf`, `.png`, `.jpg`, `.jpeg`), customise handwriting/ink/paper/layout, preview live, and download a handwritten-style PDF.
Stack: Flask · python-docx · Pillow · pypdfium2 · Tesseract OCR (pytesseract) · vanilla JS · Lucide icons.
New: **multi-colour pens** – use any number of pen colours in one document (see below).

## Run locally
```bash
python -m venv venv && source venv/bin/activate      # Windows: venv\Scripts\activate
pip install -r requirements.txt
python static/fonts/download_fonts.py                # only if fonts are missing
python make_sample.py                                # optional: creates sample.docx
python app.py                                        # open http://127.0.0.1:5000
```

### OCR (scanned PDFs and images)
PDFs that contain real text, and `.docx` files, need nothing extra. **Images and scanned PDFs need the Tesseract engine**
installed on the machine (the `pytesseract` Python package is already in `requirements.txt`):
```bash
sudo apt install tesseract-ocr          # Debian/Ubuntu     |  brew install tesseract   (macOS)
                                        # Windows: install from https://github.com/UB-Mannheim/tesseract/wiki and add it to PATH
```
Without it the app still runs; uploading an image/scan just shows "Text recognition (OCR) isn't available on this server".
Optional env vars: `OCR_LANG` (default `eng`, e.g. `eng+hin` if that language data is installed), `OCR_TIMEOUT` (seconds per page, default 60).
Deploy notes: `Aptfile` (Heroku apt buildpack) installs Tesseract; `Dockerfile` is provided for hosts like Render whose
native Python runtime cannot apt-install (set `runtime: docker` in `render.yaml`).

## Structure
```
app.py                     Flask routes, validation, cleanup
utils/document_reader.py   .docx → blocks (headings/paragraphs)
utils/extractors.py        upload validation + PDF text extraction + image pre-processing/OCR → the same blocks
utils/pens.py              pen palette, HEX/RGB parsing, colour-tag parser (multi-colour pens)
utils/handwriting_engine.py  text → paper images (jitter, rotation, ruled-line alignment, per-word pen colour)
utils/pdf_generator.py     pages → streaming, atomic, self-verifying multi-page PDF writer + validator
own_handwriting.py         "My Own Handwriting" Flask blueprint (/api/hw/*): template, sample upload, profiles
utils/profiles.py          profile + sample storage, validation (type/size/corruption), per-visitor ownership
utils/template_pdf.py      printable handwriting template (vector A4 PDF, ReportLab)
static/{css/own_handwriting.css,js/own_handwriting.js}   UI for the new section
tests/test_upload_formats.py    PDF/image/OCR/validation tests (OCR ones skip if Tesseract is absent)
tests/test_own_handwriting.py   backend tests (python -m unittest discover -s tests -v)
templates/index.html       landing page + converter (single page)
static/{css,js,fonts}      UI, logic, OFL handwriting fonts
```

## My Own Handwriting (Part 1 of 3: template, samples, profiles)
Open it from the **My Own Handwriting** card under the hero or the **My Handwriting** nav link (`/#my-handwriting`).
1. **Create** – type a profile name (or pick a suggestion). 2. **Download template** – 3-page A4 PDF: A-Z, a-z, 0-9, punctuation, symbols, sample
sentences, a writing box with baseline/x-height/cap guides per character, and four black corner squares (registration marks for Part 2). **Print** opens it inline.
3. **Upload** – drag & drop or browse, PNG / JPG / JPEG / PDF, many files, per-file progress, previews, remove, “Add more samples”.
4. **Save Profile** – needs a name and at least one sample. **Process Handwriting** is disabled until a sample is uploaded; in Part 1 it only answers “coming soon”.
5. **Saved Profiles** – name, creation date, sample count, Preview / Edit / Delete.

**Validation** (client *and* server): extension, real content type from the file's bytes (a renamed `.gif`/`.txt`/`.html` is rejected), empty files, > 8 MB, corrupted or truncated
images (fully decoded), images < 200 px or > 50 megapixels, corrupted / password-protected / > 12-page PDFs, max 20 samples and 60 MB per profile, max 30 profiles.

**Storage** (inside `$DATA_DIR`, default the project folder): `profiles/<owner_id>/<profile_id>/{profile.json, samples/<id>.<png|jpg|pdf> + <id>.json, thumbs/<id>.jpg}`.
Client file names never touch the disk (only a display name is stored). There are no accounts: each browser gets a random HttpOnly cookie (`hw_owner`) and
every route checks it, so files have **no public URL** and other visitors get 404. Unsaved drafts are deleted after 24 h; saved profiles are kept
(note: Render's free disk is ephemeral, so profiles disappear on redeploy – set `DATA_DIR` to a persistent disk for production).

| Endpoint | Purpose |
|---|---|
| `GET /api/hw/template.pdf[?inline=1]` | template download / print |
| `GET /api/hw/limits` | limits used by the UI |
| `GET, POST /api/hw/profiles` | list saved profiles / create a draft |
| `GET, DELETE /api/hw/profiles/<id>` | profile with samples / delete it |
| `POST /api/hw/profiles/<id>/save` `{name}` | name + mark as saved (also used for edits) |
| `POST /api/hw/profiles/<id>/samples` (multipart `file`) | upload one sample |
| `DELETE /api/hw/profiles/<id>/samples/<sid>` | remove a sample |
| `GET /api/hw/profiles/<id>/samples/<sid>/thumb \| file` | owner-only preview / original |
| `POST /api/hw/profiles/<id>/process` | **Part 2 hook** (currently 501 `coming_soon`) |

**Part 2 hooks:** `app.blueprints["own_handwriting"].store` is a `ProfileStore` with `samples(owner, pid)` and `sample_path(owner, pid, sid)` (original files);
`profile.json` already has `processing: {state, engine, updated}` (`not_started | queued | processing | ready | failed`) and the Saved Profiles cards display that state.

## Multi-colour pens
**Pen settings** (left panel): 8 preset pens + a custom colour (colour picker, HEX or R/G/B). The *active pen* is what the apply buttons use.
* **Roles** – Body text, Headings, Important notes, Definitions, Highlighted: press **Set** to give a role the active pen. Headings follow the
  body pen until you set them. Changing a role recolours every line that uses it, live.
* **Colour parts of my text** opens the editor. Select words → **Apply <pen>** colours just those words; put the cursor in a line (or select
  several lines) → the whole paragraph. **Important / Definition / Highlight** apply a role instead of a fixed colour. **Clear** removes colour.
* **Pen thickness** (thin / medium / thick), **Ink intensity**, **Natural variation** (pressure + tiny shade drift per word),
  **Marker highlighting** (off / only “Highlighted” text / all coloured text – a translucent band in the pen colour behind the writing).
* Existing **Handwriting style** applies to every pen. The **Pens in use** legend under the preview lists the colours really present.

Colours are stored in the text as small tags, so they are saved with the document and survive every edit (you can also type them by hand):
```
{red} Whole line in red          Some {green}words{/} in green          ## {black} A black heading
{#00a3a3}custom HEX{/}   {rgb(120,0,200)}custom RGB{/}   {important} role pen   (roles: body heading important definition highlight)
```
Unknown `{words}` are ordinary text. **How it reaches the PDF:** the editor text (tags included) → `doc.json` → `iter_pages()` paints every word in
its own pen onto the page image → that same image is the preview PNG *and* the PDF page (the PDF writer embeds lossless RGB pages), so the
preview and the downloaded PDF are pixel-identical and colours cannot change between the two. API options added to `POST /api/render`:
`ink` (body pen: name/#hex/rgb), `pens` {heading, important, definition, highlight}, `thickness`, `ink_intensity` (40-100),
`ink_variation` (0-10), `highlight` (off|marked|colored); everything is validated server-side.

## How the handwriting effect works
Print-like styles are drawn **per character**, connected scripts **per word**. Each glyph gets random baseline offset,
rotation, spacing and ink opacity scaled by the *Irregularity* slider. Text is aligned to the ruled lines, wrapped to
the margins, and a fixed random seed keeps the preview stable between renders.

## Adding more fonts
Drop a `.ttf` into `static/fonts/`, add one line to `STYLES` in `utils/handwriting_engine.py` (use `None` as the size to auto-match) and one `<option>` in `templates/index.html`.

## Security
Extension **and** content (OOXML zip) validation, 10 MB limit, generated IDs (client filenames never touch disk),
uploads deleted right after text extraction, each PDF gets a unique UUID name and is served only through a validated route (path traversal blocked); files idle for 6 hours are auto-purged (set `FILE_TTL_HOURS` to change) and never while a download is in progress, no server paths in errors, documents are only parsed, never executed.

## Deploy on Render
1. Push this folder to GitHub.
2. Render → **New → Web Service** → pick the repo (or **Blueprint** to use `render.yaml`).
3. Build: `pip install -r requirements.txt && python static/fonts/download_fonts.py`
4. Start: `gunicorn app:app --timeout 300 --workers 2`
5. Deploy. (Free-tier disk is ephemeral, which suits the no-storage design.)

## How the PDF download is kept reliable
* Pages stream from the renderer straight into the PDF file (flat memory, ~100 MB even for 60 pages). The PDF is written to `*.pdf.tmp`,
  fsync'ed, **fully verified** (xref offsets, stream lengths, page count, every page image inflated) and only then renamed into place.
* The download route re-checks the file structure on every request and answers with JSON (never a fake PDF) if anything is wrong.
* The browser fetches the file, checks status, `Content-Type`, `Content-Length`, the `%PDF-` header and `%%EOF` tail, and only then saves it.
* Storage defaults to `uploads/` + `outputs/` next to `app.py`; set `DATA_DIR` to relocate it. If that folder is read-only the app falls
  back to the system temp dir. Render's disk is ephemeral and per-instance: after a restart/redeploy old documents are gone and the user gets
  a clear "session expired" message. Run a single instance (the free-tier default) so every request sees the same files.

## Roadmap
Tables/images (extend `document_reader.py`), bold/italic runs, more fonts, user-uploaded handwriting (Part 1 done: upload + profiles; Parts 2-3: processing + conversion), page numbers/headers.


## Production deployment on Render

This project is prepared for Render using Docker because image/scanned-PDF OCR requires the Tesseract system package. In Render, create a **Web Service**, connect the GitHub repository, select **Docker** as the runtime, and deploy the included `Dockerfile`/`render.yaml`. Render's Flask deployment docs also recommend Gunicorn for production. 

For the **My Own Handwriting** profiles to survive redeploys/restarts, attach a Render persistent disk and mount it at `/var/data`; the included `DATA_DIR=/var/data` configuration is ready for that setup. Without persistent storage, saved profiles can disappear because Render's default filesystem is ephemeral.

After deployment, verify:
- `/health`
- `/robots.txt`
- `/sitemap.xml`
- the main converter
- DOCX/PDF/image upload
- PDF download
- My Own Handwriting profile creation and processing
