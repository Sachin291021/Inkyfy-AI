"""Extracts headings/paragraphs from a .docx. Tables & images are a future enhancement:
iterate doc.element.body children here and emit {"type": "table"/"image"} blocks."""
import re
import zipfile
from docx import Document

from .pens import strip_tags

MAX_UNZIPPED = 100 * 1024 * 1024  # zip-bomb guard


def validate_docx(path):
    """Content check (not just the extension): must be a real OOXML zip."""
    if not zipfile.is_zipfile(path):
        raise ValueError("This file is not a valid Word (.docx) document.")
    with zipfile.ZipFile(path) as z:
        if "word/document.xml" not in z.namelist():
            raise ValueError("This file is not a valid Word (.docx) document.")
        if sum(i.file_size for i in z.infolist()) > MAX_UNZIPPED:
            raise ValueError("The document is too large to process.")


def read_docx(path):
    """Returns blocks: {"text": str, "level": 0-3}; level 0 = body, 1-3 = heading; empty text = blank line."""
    validate_docx(path)
    blocks, prev_blank = [], True
    for p in Document(path).paragraphs:
        text = " ".join(p.text.split())
        if not text:
            if not prev_blank:
                blocks.append({"text": "", "level": 0})
            prev_blank = True
            continue
        prev_blank = False
        name = p.style.name if p.style is not None else ""
        level = 1 if name == "Title" else (int(name[-1]) if name.startswith("Heading") and name[-1] in "123" else 0)
        blocks.append({"text": text, "level": level})
    return blocks


def blocks_to_text(blocks):
    """Editable plain-text form: one paragraph per line, blank line = spacing, '#'/'##'/'###' prefix = heading,
    optional pen tags like {red} (see pens.py) are kept exactly as typed."""
    return "\n".join(("#" * b["level"] + " " if b["level"] else "") + b["text"] for b in blocks)


def text_to_blocks(text):
    """Inverse of blocks_to_text (used when the user edits the converted text)."""
    blocks, prev_blank = [], True
    for line in text.replace("\r", "").split("\n"):
        line, level = line.strip(), 0
        m = re.match(r"^(#{1,3})\s+(.*)$", line)
        if m:
            level, line = len(m.group(1)), m.group(2)
        line = " ".join(line.split())
        if not strip_tags(line).strip():           # empty, or nothing but pen tags -> blank line
            if not prev_blank:
                blocks.append({"text": "", "level": 0})
            prev_blank = True
            continue
        prev_blank = False
        blocks.append({"text": line, "level": level})   # pen tags such as {red} stay in the text (see pens.py)
    return blocks
