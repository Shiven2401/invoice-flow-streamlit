"""Document reading + field extraction.

Two layers, deliberately separated:

  read_document()   PDF text layer if there is one, otherwise OCR (RapidOCR with
                    PP-OCRv6, runs locally) on an enhanced grayscale copy of the page.
                    Also renders page images so a vision model can look at the
                    original, not just the OCR text.
  extract_fields()  'llm'   -> Claude reads the page image(s) + text, returns JSON
                    'rules' -> deterministic regex/layout heuristics (no network)
                    'auto'  -> llm if ANTHROPIC_API_KEY is set, else rules

Whatever produced the fields, ground() then checks that the important values
(total, invoice number, date, GSTIN) literally appear in the document text, so a
model that invents a number is caught instead of trusted.
"""
import base64
import hashlib
import importlib.util
import io
import json
import os
import re
import time
from datetime import date, datetime
from pathlib import Path

import cv2
import numpy as np
import pdfplumber
import pymupdf
from loguru import logger
from PIL import Image, ImageOps

from . import config

IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
OCR_TARGET_SIDE = 2000   # px; small photos are enlarged to this. The engine reads at most this size, so going past it only costs time
DET_SIDE = 1280          # px; text is *found* on a copy no longer than this (the slow half), then read from the full-size page
PAPER_FILLS_PHOTO = 0.6  # text covering more of the frame than this isn't worth a second, cropped read
SECOND_LOOK_BELOW = 0.9  # a box read with less confidence than this is read again with room around it (handwriting, mostly)
SECOND_LOOK_MAX = 40     # at most this many per page, least confident first, so a blurry page can't take minutes
MONTHS ={m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


# ----------------------------------------------------------------- reading --

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


_engine = None


def ocr_available():
    return importlib.util.find_spec("rapidocr") is not None


def _ocr_engine():
    """PP-OCRv6 (bundled with rapidocr 3, so it works offline). Much better than v4 at handwriting:
    on a handwritten physio bill it reads the amounts and the total that v4 turned into '151' and '4y5qo'."""
    global _engine
    if _engine is None:
        from rapidocr import RapidOCR
        _engine = RapidOCR(params={"Global.log_level": "warning", "Global.max_side_len": OCR_TARGET_SIDE,
                                   "Det.limit_type": "max", "Det.limit_side_len": DET_SIDE})
    return _engine


def _read(page):
    """OCR one page: [(corners, text, score)], corners clockwise from top-left."""
    # the engine keeps per-call options, so name all three: a second look below switches detection off
    out = _ocr_engine()(page, use_det=True, use_cls=True, use_rec=True)
    if out.boxes is None:
        return []
    return [(box.tolist(), tidy_ocr(text), float(score)) for box, text, score in zip(out.boxes, out.txts, out.scores)]


def _padded_crop(page, box, pad=0.3):
    """The box straightened out, with room around it in its own direction (it may sit on a tilted line)."""
    b = np.array(box, np.float32)
    across, down = b[1] - b[0], b[3] - b[0]
    w, h = float(np.linalg.norm(across)), float(np.linalg.norm(down))
    if w < 1 or h < 1:
        return None
    dx, dy = across / w * h * pad, down / h * h * pad
    quad = np.array([b[0] - dx - dy, b[1] + dx - dy, b[2] + dx + dy, b[3] - dx + dy], np.float32)
    size = (int(w + 2 * h * pad), int(h + 2 * h * pad))
    to = np.array([[0, 0], [size[0], 0], [size[0], size[1]], [0, size[1]]], np.float32)
    return cv2.warpPerspective(page, cv2.getPerspectiveTransform(quad, to), size, borderMode=cv2.BORDER_REPLICATE)


def _second_look(page, boxes):
    """Handwriting overshoots the tight box the detector draws, so loops and tails get cut off and
    the model guesses. Unsure boxes are read again from a crop with room around them; the new
    reading is kept only when the model is clearly surer of it. Paragraph-long lines are skipped:
    they're terms and conditions, and the slowest to read."""
    unsure = sorted((score, i) for i, (box, _, score) in enumerate(boxes) if score < SECOND_LOOK_BELOW
                    and np.linalg.norm(np.subtract(box[1], box[0])) < 25 * np.linalg.norm(np.subtract(box[3], box[0])))
    for score, i in unsure[:SECOND_LOOK_MAX]:
        crop = _padded_crop(page, boxes[i][0])
        if crop is None:
            continue
        out = _ocr_engine()(crop, use_det=False, use_cls=False, use_rec=True)
        text, new = tidy_ocr(out.txts[0]) if out.txts else "", float(out.scores[0]) if out.scores else 0.0
        if text and new >= score + 0.05:
            boxes[i] = (boxes[i][0], text, new)
    return boxes


def tidy_ocr(text):
    """Writing on a dotted fill-in line comes back with the dots: 'Date:30..-06-26', 'M/s......Ravi'."""
    text = re.sub(r"(?<=[\dA-Za-z])\.{2,}(?=[-/]\d)|(?<=\d[-/])\.{2,}(?=\d)", "", text)   # beside a date's dash
    # in place of one: 30..06-26, 30-06..26
    text = re.sub(r"\b(\d{1,2})\.{2,}(\d{1,2})([-/])(\d{2,4})\b", r"\1\3\2\3\4", text)
    text = re.sub(r"\b(\d{1,2})([-/])(\d{1,2})\.{2,}(\d{2,4})\b", r"\1\2\3\2\4", text)
    return re.sub(r"\s*\.{3,}\s*", " ", text).strip()


def _anthropic_key():
    return os.getenv("ANTHROPIC_API_KEY", "").strip()


def llm_key_problem():
    """Why the ANTHROPIC_API_KEY that is set can't be used, or None (also None when there is none, or Gemini is used)."""
    key = _anthropic_key()
    if key and not key.startswith("sk-ant-") and not config.GEMINI_API_KEY:
        return "ANTHROPIC_API_KEY doesn't look like an Anthropic key (those start with 'sk-ant-'), so Claude is not used"
    return None


def llm_provider():
    """('Gemini' | 'Claude', model id) for the vision model that reads documents, or (None, None)."""
    if config.GEMINI_API_KEY:
        return "Gemini", config.GEMINI_MODEL
    # a key from another provider would only be refused, after sending the invoice images out with it
    if _anthropic_key().startswith("sk-ant-"):
        return "Claude", config.EXTRACT_MODEL
    return None, None


def llm_available():
    return llm_provider()[0] is not None


_gemini_today = [None, 0]  # [date, documents sent]; the one worker thread is the only caller, and a restart starts the count again


def _within_daily_limit():
    """On a public site anyone can upload under the owner's key: past GEMINI_DAILY_LIMIT, read with the built-in rules."""
    if not config.GEMINI_DAILY_LIMIT or llm_provider()[0] != "Gemini":
        return True
    today = date.today().isoformat()
    if _gemini_today[0] != today:
        _gemini_today[:] = [today, 0]
    if _gemini_today[1] >= config.GEMINI_DAILY_LIMIT:
        return False
    _gemini_today[1] += 1
    return True


def enhance(img: Image.Image):
    """Prepare a photo for OCR: grayscale, sized to what the engine reads (small text enlarged,
    a big photo shrunk first so nothing is done to pixels the engine would throw away), contrast stretched."""
    g = ImageOps.grayscale(ImageOps.exif_transpose(img))
    scale = min(OCR_TARGET_SIDE / max(g.size), 3)
    if abs(scale - 1) > 0.02:
        g = g.resize((int(g.width * scale), int(g.height * scale)), Image.LANCZOS)
    return np.array(ImageOps.autocontrast(g, cutoff=1))


def layout_rows(boxes):
    """Rebuild printed rows from OCR text boxes so the parsers can read them line by line.

    Each box is (corners, text, score), corners clockwise from top-left. Rows are
    grouped after removing the tilt, because in a phone photo the two ends of a row
    sit at different heights. Paper curves, so the tilt is measured from the text
    around each box, not once for the page. Cells in a row stay 4 spaces apart.
    """
    if not boxes:
        return ""
    height = float(np.median([b[3][1] - b[0][1] for b, *_ in boxes]))
    middle = float(np.median([b[0][0] for b, *_ in boxes]))
    mid_y = lambda b: (b[0][1] + b[3][1]) / 2
    # short boxes round to a misleading slope, so only text at least two heights wide measures it
    slopes = [(mid_y(b), (b[1][1] - b[0][1]) / (b[1][0] - b[0][0])) for b, *_ in boxes if b[1][0] - b[0][0] > height * 2]

    def tilt_at(y):
        near = [s for sy, s in slopes if abs(sy - y) < height * 8] or [s for _sy, s in slopes]
        return float(np.median(near)) if near else 0.0

    rows = []
    cells = sorted((mid_y(b) - tilt_at(mid_y(b)) * (b[0][0] - middle), b[0][0], text) for b, text, *_ in boxes)
    for y, x, text in cells:
        if rows and y - rows[-1][0] < height * 0.6:
            rows[-1][1].append((x, text))
        else:
            rows.append((y, [(x, text)]))
    return "\n".join("    ".join(t for _, t in sorted(row)) for _, row in rows)


def _paper_crop(img, shape, boxes):
    """The area holding the text, if it fills only part of the photo. Otherwise None."""
    height, width = shape
    xs, ys = [p[0] / width for b, *_ in boxes for p in b], [p[1] / height for b, *_ in boxes for p in b]
    left, right, top, bottom = min(xs), max(xs), min(ys), max(ys)
    if (right - left) * (bottom - top) > PAPER_FILLS_PHOTO:
        return None
    pad_x, pad_y = (right - left) * 0.06, (bottom - top) * 0.04
    return img.crop((int(max(left - pad_x, 0) * img.width), int(max(top - pad_y, 0) * img.height),
                     int(min(right + pad_x, 1) * img.width), int(min(bottom + pad_y, 1) * img.height)))


def _ocr(img: Image.Image) -> str:
    if not ocr_available():
        return ""
    img = ImageOps.exif_transpose(img)
    page = enhance(img)
    boxes = _read(page)
    if boxes:  # a bill lying on a table is small in the frame: read it again, cropped to the paper and enlarged
        crop = _paper_crop(img, page.shape, boxes)
        if crop:
            closer_page = enhance(crop)
            closer = _read(closer_page)
            if closer and len(closer) >= len(boxes):
                page, boxes = closer_page, closer
    return layout_rows(_second_look(page, boxes))


def _jpeg_b64(img: Image.Image, max_side=1800):
    img = ImageOps.exif_transpose(img).convert("RGB")
    if max(img.size) > max_side:
        r = max_side / max(img.size)
        img = img.resize((int(img.width * r), int(img.height * r)), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode()


def read_document(path):
    """Return {text, method, pages, page_images, ...}. method: text_layer | ocr | unreadable."""
    p = Path(path)
    ext = p.suffix.lower()
    info = dict(filename=p.name, ext=ext, bytes=p.stat().st_size, sha256=sha256_file(p),
                pages=1, method="unreadable", text="", page_images=[], notes=[])
    if ext == ".pdf":
        doc = pymupdf.open(p)
        info["pages"] = len(doc)
        text = ""
        with pdfplumber.open(p) as pdf:
            for pg in pdf.pages[:6]:
                text += (pg.extract_text(layout=True) or "") + "\n"
        if len(text.strip()) >= 40:
            info["method"] = "text_layer"
            info["text"] = text
        else:
            info["notes"].append("no text layer - scanned image")
        for i, pg in enumerate(doc):
            if i >= 3:
                break
            pix = pg.get_pixmap(dpi=150)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            info["page_images"].append(_jpeg_b64(img))
            if info["method"] != "text_layer":
                hi = pg.get_pixmap(dpi=200)
                himg = Image.frombytes("RGB", (hi.width, hi.height), hi.samples)
                info["text"] += _ocr(himg) + "\n"
        if info["method"] != "text_layer":
            info["method"] = "ocr" if info["text"].strip() else "unreadable"
    elif ext in IMG_EXT:
        img = Image.open(p)
        info["page_images"].append(_jpeg_b64(img))
        info["text"] = _ocr(img)
        info["method"] = "ocr" if info["text"].strip() else "unreadable"
        if not ocr_available():
            info["notes"].append("OCR engine not installed - image can only be read by the vision model")
    else:
        info["notes"].append(f"unsupported file type {ext}")
    if info["method"] == "unreadable":
        logger.warning("no text recovered from {!r} (OCR available: {})", p.name, ocr_available())
    return info


# ------------------------------------------------------------ small parsers --

AMT_RE = re.compile(
    r"(?<![\w/\-.])(?:Rs\.?\s?|INR\s?|₹\s?)?(\d[\d,]*\.\d{2}|\d{1,3}(?:,\d{2,3})+|\d{4,})(?![\w/\-])")


def to_num(s):
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    s = re.sub(r"[^\d.]", "", str(s).replace(",", ""))
    try:
        return float(s) if s not in ("", ".") else None
    except ValueError:
        return None


def amounts_in(line):
    return [to_num(m) for m in AMT_RE.findall(line)]


def norm_alnum(s):
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def parse_date(s):
    """Return ISO date or None. Day-first (Indian convention) for ambiguous numeric dates."""
    if not s:
        return None
    s = str(s).strip()
    pats = [
        (r"\b(\d{4})-(\d{2})-(\d{2})\b", "ymd"),
        (r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})(?:\b|(?=\d{1,2}:\d{2}))", "dmy"),  # OCR can glue the time on: 28-11-202510:57PM
        (r"\b(\d{1,2})[ \-]([A-Za-z]{3,9})[ \-,.]*(\d{4})\b", "dMy"),
        (r"\b([A-Za-z]{3,9})\.? (\d{1,2}),? (\d{4})\b", "Mdy"),
        (r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2})\b", "dmy2"),
        (r"\b(\d{1,2})[ \-]([A-Za-z]{3,9})[ \-,.]*(\d{2})\b", "dMy2"),
    ]
    for pat, kind in pats:
        m = re.search(pat, s)
        if not m:
            continue
        a, b, c = m.groups()
        try:
            if kind == "ymd":
                y, mo, d = int(a), int(b), int(c)
            elif kind == "dmy":
                d, mo, y = int(a), int(b), int(c)
            elif kind == "dmy2":
                d, mo, y = int(a), int(b), 2000 + int(c)
            elif kind == "dMy":
                d, mo, y = int(a), MONTHS.get(b[:3].lower()), int(c)
            elif kind == "dMy2":
                d, mo, y = int(a), MONTHS.get(b[:3].lower()), 2000 + int(c)
            else:
                mo, d, y = MONTHS.get(a[:3].lower()), int(b), int(c)
            if mo is None:
                continue
            return date(y, mo, d).isoformat()
        except ValueError:
            continue
    return None


def all_dates(text):
    out = set()
    for line in text.splitlines():
        for chunk in re.findall(r"\d{1,4}[/\-. ][A-Za-z0-9]{1,9}[/\-., ]+\d{2,4}|[A-Za-z]{3,9}\.? \d{1,2},? \d{4}", line):
            d = parse_date(chunk)
            if d:
                out.add(d)
    return out


# ---------------------------------------------------------- rule extractor --

COMPANY_RE = re.compile(
    r"\b(pvt|private|ltd|limited|llp|inc|corp|co\.?|company|services|stationers|enterprises|traders|industries|"
    r"solutions|logistics|management|supplies|systems|associates|agency|works|marketing|motors|store|centre|center|"
    r"cafe|café|bistro|restaurant|hotel|bakery|kitchen|dhaba|foods?)\b", re.I)
SKIP_TOP = re.compile(r"^(tax\s*)?invoice\b|^\W*[\w-]*invoice[\w-]*\W*$|^\W*original|^\W*duplicate|^copy|^page\s*\d|^bill\s*of\s*supply|^e-?invoice|^gstin|^\W*(?:food\s*)?licen[cs]e|^\W*fssai", re.I)


GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
# after a GSTIN label no boundary is needed: OCR can glue words on either side ("PAN:AAMCS8857LGSTIN:08AAMC...1ZCCIN:").
# Two patterns, searched separately, because in that text a bare 15-character match would swallow the label itself.
GSTIN_LABELLED = re.compile(r"GSTIN(?:/UIN)?\W*([0-9A-Z]{15})")
GSTIN_BARE = re.compile(r"(?<![A-Z0-9])[0-9A-Z]{15}(?![A-Z0-9])")
_TO_DIGIT = str.maketrans("OQDIZSB", "0001258")
_TO_ALPHA = str.maketrans("0158", "OISB")


def fix_gstin(tok):
    """Repair classic OCR confusions by position (O/0, I/1, S/5, B/8). None if it still isn't a GSTIN."""
    if len(tok) != 15:
        return None
    t = list(tok)
    for i in (0, 1, 7, 8, 9, 10):
        t[i] = t[i].translate(_TO_DIGIT)
    for i in (2, 3, 4, 5, 6, 11):
        t[i] = t[i].translate(_TO_ALPHA)
    t[13] = "Z" if t[13] in "2Z" else t[13]
    s = "".join(t)
    return s if GSTIN_RE.match(s) else None


def _lines(text):
    """Non-blank lines with spacing tidied. A wide gap separates columns, so it stays as 4 spaces."""
    return [re.sub(r"[ \t]+", lambda m: "    " if len(m.group()) >= 4 else " ", l).strip() for l in text.splitlines() if l.strip()]


def _cell_below(lines, label):
    """Value printed under its label in a boxed header grid. Returns (value, source text) or (None, None).

    Grid rows share a right edge but the row below may also carry text from the
    left-hand address block, so cells are lined up counting from the right.
    """
    for above, below in zip(lines, lines[1:]):
        heads, cells = above.split("    "), below.split("    ")
        col = next((i for i, c in enumerate(heads) if label.fullmatch(c)), None)
        if col is not None and len(cells) - len(heads) + col >= 0:
            return cells[len(cells) - len(heads) + col], above + " | " + below
    return None, None


def classify_doc(text):
    t = text.lower()
    if re.search(r"not a gst invoice|confirmation receipt|payment successful|transaction (status|details)|bill payment", t):
        return "receipt"
    if re.search(r"credit\s*note", t):
        return "credit_note"
    if re.search(r"quotation|\bestimate\b|pro-?forma", t):
        return "quotation"
    if re.search(r"tax\s*invoice|\binvoice\b", t):
        return "invoice"
    if re.search(r"\b(cash\s*)?receipt\b", t):
        return "receipt"
    if re.search(r"\bbill\b|\bbill no\b|\bbill month\b", t):
        return "bill"
    return "other"


INVOICE_TITLE = re.compile(r"^\W*(?:(?:(?:tax|gst|retail|sales|commercial|e)[\s-]*)?invoice|bill(?:\s*of\s*supply)?|credit\s*note)"
                           r"\W*(?:(?:original|duplicate|triplicate|copy|for\s+recipient)\b.*)?$", re.I)


def _invoice_shape(f, lines):
    """Is this an invoice, or does it just talk about invoices? Returns invoice | unconfirmed | other.

    The word alone proves nothing and can be missing from a real bill (blurred small
    print, an insurance schedule), so this looks at what the document carries:

      invoice      a title at the top, or details only a bill has: a labelled number
                   with a date or amounts, tax lines with a total, or a GSTIN and date
                   with tax lines or line items
      unconfirmed  two or more of those details, but not enough to be sure. A person
                   looks: a wrong guess here must not cost a real bill its place
      other        at most one, so it is rejected
    """
    if any(INVOICE_TITLE.match(cell) for l in lines[:15] for cell in l.split("    ")):
        return "invoice"
    tax, items = f["tax_amount"] is not None, bool(f["line_items"])
    total = f["total"] is not None or f["subtotal"] is not None
    number, dated, gstin = bool(f["invoice_number"]), bool(f["invoice_date"]), bool(f["vendor_gstin"])
    if (number and (total or tax or items or dated)) or (tax and total) or (gstin and dated and (tax or items)):
        return "invoice"
    return "unconfirmed" if sum((tax, items, total, number, dated, gstin)) >= 2 else "other"


TAX_LABEL = re.compile(r"^\W*vat\b(?!\s*(?:no|reg|tin|#))|sur\s*-?\s*charge|\bcess\b|service\s*charge|(?:cgst|sgst|igst|utgst)\b", re.I)


def _no_pct(s):
    return re.sub(r"\d+(?:\.\d+)?\s*%", " ", s)


def _labelled_values(line, label):
    """[(label, label + its value text, amounts)] for each time the label appears in a row.

    A row can carry two columns, so a label's value is what follows it in its own cell
    plus the number-only cells after that, never text further along: in
    "SGST(9%)    2,092    5. ... deductible of INR 1000" the tax is 2,092. Two labels in
    one cell ("CGST 9% 450.00 SGST 9% 450.00") each get the numbers up to the next one.
    """
    out, cells = [], line.split("    ")
    for k, cell in enumerate(cells):
        hits = list(label.finditer(cell))
        tail = []
        for nxt in cells[k + 1:]:
            if re.search(r"[A-Za-z]{2}", re.sub(r"Rs\.?|INR", "", nxt)):
                break
            tail.append(nxt)
        for j, m in enumerate(hits):
            value = cell[m.end():hits[j + 1].start() if j + 1 < len(hits) else len(cell)]
            if j == len(hits) - 1:
                value = " ".join([value] + tail)
            out.append((m.group().strip(" \t:-"), m.group() + value, amounts_in(_no_pct(value))))
    return out


def _first_label_amount(lines, patterns, exclude=None):
    for pat in patterns:
        for i, l in enumerate(lines):
            m = re.search(pat, l, re.I)
            if m and not (exclude and re.search(exclude, l, re.I)):
                a = amounts_in(l[m.end():])  # after the label: OCR can glue the two, "Net Amount12446.00"
                if a:
                    return a[-1], l
                # a value alone on the next line, not the digits inside something else such as "Tin:08869410"
                if i + 1 < len(lines) and not re.search(r"[A-Za-z]{2}", re.sub(r"Rs\.?|INR", "", lines[i + 1])):
                    a = amounts_in(lines[i + 1])
                    if a:
                        return a[0], l + " | " + lines[i + 1]
    return None, None


def rules_extract(text):
    lines = _lines(text)
    f = dict(doc_type=classify_doc(text), vendor_name=None, vendor_gstin=None, buyer_name=None,
             invoice_number=None, invoice_date=None, po_reference=None, currency="INR",
             subtotal=None, tax_amount=None, tax_rate=None, tax_included=False, total=None,
             line_items=[], description_text="")
    ev, conf = {}, {}

    # vendor name: first company-looking line near the top, first column only
    for l in lines[:14]:
        if SKIP_TOP.search(l) or sum(c.isalpha() for c in l) < 4:
            continue
        seg = re.split(r"\s{4,}", l)[0]
        seg = re.sub(r"\s*\b(TAX\s+)?INVOICE\b.*$", "", seg, flags=re.I).strip(" -|:")
        if COMPANY_RE.search(seg):
            f["vendor_name"], conf["vendor_name"], ev["vendor_name"] = seg, "high", l
            break
    if not f["vendor_name"]:
        for l in lines[:8]:
            seg = re.sub(r"\s*\b(TAX\s+)?INVOICE\b.*$", "", re.split(r"\s{4,}", l)[0], flags=re.I).strip(" -|:")
            if sum(c.isalpha() for c in seg) >= 4 and not SKIP_TOP.search(seg):
                f["vendor_name"], conf["vendor_name"], ev["vendor_name"] = seg, "low", l
                break

    # vendor GSTIN: the first valid GSTIN ABOVE the Bill-To block (the buyer's GSTIN sits below it)
    bill_idx = next((i for i, l in enumerate(lines) if re.match(r"^(bill(ed)?\s*to|buyer|ship(ped)?\s*to|consignee)\b", l, re.I)), len(lines))
    cands = []
    for i, l in enumerate(lines):
        for tok in GSTIN_LABELLED.findall(l.upper()) + GSTIN_BARE.findall(l.upper()):
            fixed = fix_gstin(tok)
            if fixed:
                cands.append((i, fixed, fixed != tok, l))
    pick = next((c for c in cands if c[0] < bill_idx), None) or (cands[0] if cands else None)
    if pick:
        f["vendor_gstin"], conf["vendor_gstin"], ev["vendor_gstin"] = pick[1], ("medium" if pick[2] else "high"), pick[3]

    # invoice number
    num_pat = re.compile(r"(?:[il]nvoice|[il]nv|bill)\s*(?:no\.?|number|num|#|id)\s*[:#\-]?\s*([A-Z0-9][A-Z0-9/\-_.]*[A-Z0-9])", re.I)  # OCR reads I as l
    for l in lines:
        m = num_pat.search(l)
        if m and re.search(r"\d", m.group(1)) and m.group(1).lower() not in ("date",):
            f["invoice_number"], conf["invoice_number"], ev["invoice_number"] = m.group(1).upper(), "high", l
            break
    if not f["invoice_number"]:
        val, src = _cell_below(lines, re.compile(r"(?:[il]nvoice|[il]nv|bill)\s*(?:no\.?|number|num|#)\W*", re.I))
        if val and re.search(r"\d", val) and re.fullmatch(r"[A-Z0-9][A-Z0-9/\-_.]*[A-Z0-9]", val, re.I):
            f["invoice_number"], conf["invoice_number"], ev["invoice_number"] = val.upper(), "medium", src

    # invoice date: labelled only - a missing date must stay missing
    # A header row can hold two dates ("Invoice Date 03 Nov 2026    PO Date 15 Oct 2026"), so the value is read from
    # after the label up to the next label for another kind of date, and a "Due Date" or "PO Date" label is skipped
    other = r"due|p\.?o\.?\s*date|order\s*date|delivery|valid|expir"
    for i, l in enumerate(lines):
        for m in re.finditer(r"invoice\s*date|bill\s*date|date\s*of\s*(?:issue|invoice)|\bdated?\b", l, re.I):
            if re.search(rf"(?:{other})\W*$", l[:m.start()], re.I):
                continue
            seg = re.split(other, l[m.end():], maxsplit=1, flags=re.I)[0].strip(" :.-")
            d = parse_date(seg) or (parse_date(lines[i + 1]) if not re.search(r"\d", seg) and i + 1 < len(lines) else None)
            if d:
                f["invoice_date"], conf["invoice_date"], ev["invoice_date"] = d, "high", l
                break
        if f["invoice_date"]:
            break

    # PO reference: explicit forms only
    m = re.search(r"\bP[O0]\s*[-–]?\s*(\d{4})\s*[-–]?\s*(\d{3,5})\b", text)
    if m:
        f["po_reference"], conf["po_reference"] = f"PO-{m.group(1)}-{m.group(2)}", "high"
        ev["po_reference"] = next((l for l in lines if m.group(0) in l), "")
    else:
        m = re.search(r"(?:P\.?\s?O\.?|purchase\s*order)\s*(?:no\.?|number|ref(?:erence)?\.?|#)\s*[:\-]?\s*([A-Z0-9][A-Z0-9/\-_.]{3,})", text, re.I)
        if m and re.search(r"\d", m.group(1)):
            f["po_reference"], conf["po_reference"] = m.group(1).upper(), "medium"

    # buyer
    for i, l in enumerate(lines):
        if re.match(r"^(bill(ed)?\s*to|buyer)\b", l, re.I):
            cut = r"\s{4,}|\b(?:invoice|payment|po)\s*(?:no|date|ref|terms|#)\b"
            rest = re.sub(r"^(bill(ed)?\s*to|buyer)\s*(\(bill\s*to\))?\s*[:\-]?\s*", "", l, flags=re.I)
            rest = re.split(cut, rest, flags=re.I)[0].strip(" :-")
            if not rest and i + 1 < len(lines):
                rest = re.split(cut, lines[i + 1], flags=re.I)[0].strip(" :-")
            f["buyer_name"] = rest or None
            break

    # amounts. Tax comes first, because a line's place around it decides what the line means:
    # "Net Amount" or "Gross" above the tax is a pre-tax figure, and below it is the final total
    tax_sum, rate_sum, tax_lines, tax_at, other_levies = 0.0, 0.0, [], [], False
    for i, l in enumerate(lines):
        found = False
        for label, span, after in _labelled_values(l, TAX_LABEL):
            levy = not re.match(r"[csiu]t?gst", label, re.I)
            a = after or (amounts_in(_no_pct(lines[i + 1])) if levy and i + 1 < len(lines) else [])  # a long label can wrap
            r = re.search(r"(\d+(?:\.\d+)?)\s*%", span)
            if a:
                tax_sum += a[-1]
                found = True
                other_levies = other_levies or levy
                if r and not levy:
                    rate_sum += float(r.group(1))
        if found:
            tax_lines.append(l)
            tax_at.append(i)
    if not tax_lines:
        for i, l in enumerate(lines):
            if re.match(r"^\W*(output\s+)?(gst|tax)\b", l, re.I) and not re.search(r"gstin|total|incl", l, re.I):
                a = amounts_in(l)
                r = re.search(r"(\d+(?:\.\d+)?)\s*%", l)
                if a:
                    tax_sum += a[-1]; tax_lines.append(l); tax_at.append(i)
                    if r: rate_sum += float(r.group(1))
                    break
    if tax_lines:
        f["tax_amount"], conf["tax_amount"], ev["tax_amount"] = round(tax_sum, 2), "high", " ; ".join(tax_lines)
        if rate_sum and not other_levies:  # GST plus VAT has no single rate to check against
            f["tax_rate"] = rate_sum
    pre_tax = lines[:tax_at[0]] if tax_at else lines
    after_tax = lines[tax_at[-1] + 1:] if tax_at else []

    f["subtotal"], e = _first_label_amount(
        pre_tax, [r"sub\s*-?\s*total", r"taxable\s*(value|amount)", r"total\s*before\s*tax", r"amount\s*before\s*tax", r"net\s*amount"])
    if e: ev["subtotal"] = e

    f["total"], e = _first_label_amount(
        lines, [r"grand\s*total", r"amount\s*payable|total\s*payable|net\s*payable|total\s*due|balance\s*due",
                r"invoice\s*total|total\s*invoice\s*value"])
    if f["total"] is None:
        f["total"], e = _first_label_amount(after_tax or lines, [r"gr[o0u]ss\s*(?:total|amount|prem\w*)", r"\bnet\s*(?:amount|total|value)"])
    for part in ([after_tax] if after_tax else []) + [lines]:  # a "Total" printed above the tax lines is a pre-tax figure
        if f["total"] is None:
            f["total"], e = _first_label_amount(part, [r"total\s*\(?\s*incl", r"^\W*total\b(?!.*(tax|before|qty|quantity))"])
    if e:
        conf["total"], ev["total"] = "high", e

    # pre-tax value when a bill shows "Gross Amount" then a discount. "Sub Total" can also label one section of a long bill.
    # If the sums don't work, use the labelled amount that makes them work: only labelled lines qualify, because taking
    # any matching number would explain away a genuine arithmetic error.
    gross, gross_line = _first_label_amount(pre_tax, [r"gr[o0u]ss\s*(?:amount|total)"])
    discount, discount_line = _first_label_amount(pre_tax, [r"\bdiscount\b"])
    base = f["subtotal"] if f["subtotal"] is not None else gross
    slack = config.RULES["arith_abs_tol"]
    if f["total"] is not None and f["tax_amount"] is not None:
        net = round(f["total"] - f["tax_amount"], 2)
        if base is not None and discount and abs(base - discount - net) <= slack:
            f["subtotal"], f["discount"], ev["subtotal"] = round(base - discount, 2), discount, f"{gross_line or ev.get('subtotal')} ; {discount_line}"
        elif f["subtotal"] is None or abs(f["subtotal"] - net) > slack:
            for l in lines:
                if re.search(r"\bnet\b|taxable|sub\s*-?\s*total|before\s*tax", l, re.I) and any(abs(a - net) <= slack for a in amounts_in(l) if a is not None):
                    f["subtotal"], ev["subtotal"] = net, l
                    break
    if f["subtotal"] is None and gross is not None:
        f["subtotal"], ev["subtotal"] = gross, gross_line

    incl = re.search(r"incl(?:\.|uding|usive|uded)?\s*(?:of\s*)?(?:\d+(?:\.\d+)?\s*%\s*)?(?:gst|tax)|(?:gst|tax)\s*(?:is\s*)?(?:incl|included)", text, re.I)
    if incl and not tax_lines:
        f["tax_included"] = True
        if f["tax_rate"] is None:
            r = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(?:gst|tax)|(?:gst|tax)[^\n]{0,12}?(\d+(?:\.\d+)?)\s*%", text, re.I)
            if r:
                f["tax_rate"] = float(r.group(1) or r.group(2))
        conf["tax_included"] = "high"

    # line items + description text
    LINE = re.compile(r"^\s*(\d{1,2})[.)]?\s+(.+?)\s+(?:(\d{4,8})\s+)?(\d+(?:\.\d+)?)\s+([\d,]+\.\d{2})\s+([\d,]+\.\d{2})\s*$")
    ONE = re.compile(r"^\s*(\d{1,2})[.)]?\s+(.+?)\s+([\d,]+\.\d{2})\s*$")
    # GST table: description, HSN, GST %, qty + unit, [rate incl. tax], rate + unit, amount
    GST = re.compile(r"^\s*(?:\d{1,2}[.)]?\s*)?(.+?)\s+\d{4,8}\s+\S*\s?%\S*\s+(\d+(?:\.\d+)?)\s*[A-Za-z]+\s+"
                     r"(?:[\d,]+\.\d{2}\s+)?([\d,]+\.\d{2})\s*[A-Za-z]+\s+([\d,]+\.\d{2})\s*$")
    # no serial numbers, as on a restaurant bill: description, qty, rate, amount. A long description can wrap above the numbers
    PLAIN = re.compile(r"^\s*(?:(\D.*?)\s+)?(\d{1,3}(?:\.\d+)?)\s+([\d,]+\.\d{2})\s*([\d,]+\.\d{2})\s*$")
    in_table, desc_lines, wrapped = False, [], ""
    for l in lines:
        if re.search(r"\b(description|particulars)\b", l, re.I) and re.search(r"amount|qty|rate", l, re.I):
            in_table = True
            continue
        if in_table and re.search(r"sub\s*-?\s*total|grand\s*total|gross\s*amount|total\s*\(|^\W*total\b", l, re.I):
            in_table = False
        if not in_table:
            continue
        m = GST.match(l)
        if m:
            desc, qty, rate, amt = m.groups()
            f["line_items"].append(dict(description=desc.strip(), quantity=float(qty), unit_price=to_num(rate), amount=to_num(amt)))
            desc_lines.append(desc)
            continue
        m = LINE.match(l)
        if m:
            _, desc, _hsn, qty, rate, amt = m.groups()
            f["line_items"].append(dict(description=desc.strip(), quantity=float(qty), unit_price=to_num(rate), amount=to_num(amt)))
            desc_lines.append(desc)
            continue
        m = PLAIN.match(l)
        if m and abs(float(m[2]) * to_num(m[3]) - to_num(m[4])) <= 1:
            desc = (m[1] or wrapped).strip()
            f["line_items"].append(dict(description=desc, quantity=float(m[2]), unit_price=to_num(m[3]), amount=to_num(m[4])))
            desc_lines.append(desc)
            wrapped = ""
            continue
        m = ONE.match(l)
        if m:
            _, desc, amt = m.groups()
            f["line_items"].append(dict(description=desc.strip(), quantity=None, unit_price=None, amount=to_num(amt)))
            desc_lines.append(desc)
        elif re.search(r"\d\.\d{2}\s*$", l):
            f["items_incomplete"] = True  # a row with an amount that couldn't be read, so the items won't sum to the bill
        elif sum(c.isalpha() for c in l) >= 6:
            desc_lines.append(l)
            wrapped = l
    f["description_text"] = " ".join(desc_lines)
    if f["line_items"]:
        conf["line_items"] = "high"
    f["confidence"], f["evidence"] = conf, ev
    if f["doc_type"] in ("invoice", "bill", "credit_note", "other"):
        shape = _invoice_shape(f, lines)
        if shape != "invoice":
            f["doc_type"] = shape
        elif f["doc_type"] == "other":
            f["doc_type"] = "invoice"
    return f


# ---------------------------------------------------------- vision extractor --

LLM_SYSTEM = """You are an accounts-payable data-extraction engine. You read vendor invoices (clean PDFs, scans, phone photos, handwritten bills) and return ONLY a JSON object.
Rules:
- Never guess. If a field is not visible in the document, use null. A missing invoice number, date or total must stay null.
- Numbers are plain JSON numbers in the document's currency (no symbols, no thousands separators). Indian digit grouping (1,18,000.00) = 118000.00.
- Dates are ISO YYYY-MM-DD. Ambiguous numeric dates are day-first (Indian convention).
- po_reference: only a PO number that is explicitly printed. Do not infer one from descriptions.
- tax_included_in_amounts: true if the line amounts / total already include GST and no separate tax line is shown.
- doc_type is one of: invoice, credit_note, quotation, proforma, receipt, bill, other. A payment confirmation or fee receipt is 'receipt', not 'invoice'.
- confidence per field: high | medium | low (low = blurry, handwritten, or partly cut off).
Schema:
{"doc_type":str,"vendor_name":str|null,"vendor_gstin":str|null,"buyer_name":str|null,"invoice_number":str|null,"invoice_date":str|null,
"po_reference":str|null,"currency":str,"subtotal":num|null,"tax_amount":num|null,"tax_rate_percent":num|null,"tax_included_in_amounts":bool,
"total":num|null,"line_items":[{"description":str,"quantity":num|null,"unit_price":num|null,"amount":num|null}],
"confidence":{"<field>":"high|medium|low"},"notes":str|null}"""


def _ask_claude(images, prompt):
    import anthropic
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}} for b64 in images]
    resp = anthropic.Anthropic().messages.create(model=config.EXTRACT_MODEL, max_tokens=2500, temperature=0, system=LLM_SYSTEM,
                                                 messages=[{"role": "user", "content": content + [{"type": "text", "text": prompt}]}])
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def _ask_gemini(images, prompt):
    import requests
    parts = [{"inline_data": {"mime_type": "image/jpeg", "data": b64}} for b64 in images] + [{"text": prompt}]
    body = {"systemInstruction": {"parts": [{"text": LLM_SYSTEM}]}, "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
    # the key goes in a header, not the URL, so it can't turn up in an error message or a log line
    for wait in (2, 6, None):  # busy (503) or rate-limited (429) is usually brief, so try twice more before falling back
        r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent",
                          headers={"x-goog-api-key": config.GEMINI_API_KEY}, json=body, timeout=60)
        if r.status_code not in (429, 503) or wait is None:
            break
        logger.info("Gemini busy ({}), retrying in {}s", r.status_code, wait)
        time.sleep(wait)
    if not r.ok:
        raise RuntimeError(f"Gemini returned {r.status_code}: {r.text[:200]}")
    candidates = r.json().get("candidates") or []
    return "".join(p.get("text", "") for c in candidates[:1] for p in (c.get("content") or {}).get("parts", []))


def llm_extract(doc):
    txt = doc["text"].strip()
    prompt = "Extract the invoice fields from the document above."
    if txt:
        prompt += f"\n\nText recovered from the document ({doc['method']}), may contain OCR errors:\n<text>\n{txt[:12000]}\n</text>"
    ask = _ask_gemini if llm_provider()[0] == "Gemini" else _ask_claude
    raw = ask(doc["page_images"][:3], prompt)
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise ValueError("model returned no JSON")
    j = json.loads(m.group(0))
    items = []
    for li in j.get("line_items") or []:
        items.append(dict(description=li.get("description") or "", quantity=to_num(li.get("quantity")),
                          unit_price=to_num(li.get("unit_price")), amount=to_num(li.get("amount"))))
    f = dict(doc_type=(j.get("doc_type") or "other").lower(), vendor_name=j.get("vendor_name"),
             vendor_gstin=(j.get("vendor_gstin") or None), buyer_name=j.get("buyer_name"),
             invoice_number=(str(j["invoice_number"]).strip() if j.get("invoice_number") else None),
             invoice_date=parse_date(j.get("invoice_date")), po_reference=(j.get("po_reference") or None),
             currency=j.get("currency") or "INR", subtotal=to_num(j.get("subtotal")),
             tax_amount=to_num(j.get("tax_amount")), tax_rate=to_num(j.get("tax_rate_percent")),
             tax_included=bool(j.get("tax_included_in_amounts")), total=to_num(j.get("total")),
             line_items=items, description_text=" ".join(i["description"] for i in items) + " " + (j.get("notes") or ""),
             confidence=j.get("confidence") or {}, evidence={})
    if f["po_reference"]:
        m = re.search(r"P[O0]\s*[-–]?\s*(\d{4})\s*[-–]?\s*(\d{3,5})", f["po_reference"], re.I)
        if m:
            f["po_reference"] = f"PO-{m.group(1)}-{m.group(2)}"
    return f


# ------------------------------------------------------------- typed inputs --

USER_FIELDS = {"vendor_name": "Vendor name", "vendor_gstin": "GSTIN", "invoice_number": "Invoice number",
               "invoice_date": "Invoice date", "po_reference": "PO number", "total": "Total amount"}


def clean_inputs(raw):
    """Normalise details typed at upload; blanks are dropped. Raises ValueError for a value that can't be used."""
    if not isinstance(raw, dict):
        raise ValueError("Details must be a set of named fields.")
    out = {}
    for key in (*USER_FIELDS, "note"):
        v = str(raw.get(key) or "").strip()[:500]
        if not v:
            continue
        if key == "invoice_date":
            v = parse_date(v)
            if not v:
                raise ValueError(f"Could not read '{raw[key]}' as a date. Try DD-MM-YYYY.")
        elif key == "total":
            if not re.fullmatch(r"(?:Rs\.?|INR|₹)?\s*\d[\d,]*(?:\.\d+)?", v, re.I):
                raise ValueError(f"Could not read '{v}' as an amount. Try 1,18,000.00.")
            v = to_num(re.sub(r"^\D+", "", v))
        elif key == "vendor_gstin":
            v = v.upper()
            if not GSTIN_RE.match(v):
                raise ValueError(f"'{v}' is not a valid 15-character GSTIN.")
        elif key in ("invoice_number", "po_reference"):
            v = v.upper()
        out[key] = v
    return out


def apply_inputs(fields, inputs):
    """Put typed details into the extracted fields. Returns (entered, conflicts).

    entered   labels of fields the document didn't show, filled in by hand
    conflicts (label, read, typed) where the document says something else

    A typed value is not in the document, so its grounding check no longer applies.
    The caller turns both lists into findings: a person's typing is never silent.
    """
    entered, conflicts = [], []
    for key, label in USER_FIELDS.items():
        typed = inputs.get(key)
        if typed is None:
            continue
        read = fields.get(key)
        if read not in (None, "") and (abs(read - typed) < 0.01 if key == "total" else norm_alnum(read) == norm_alnum(typed)):
            continue  # agrees with the document, so the document's own evidence stands
        if read in (None, ""):
            entered.append(label)
        else:
            conflicts.append((label, read, typed))
        fields[key] = typed
        fields.setdefault("confidence", {})[key] = "user"
        fields.get("grounding", {}).pop(key, None)
    return entered, conflicts


# -------------------------------------------------------------- orchestration --

def extract_fields(doc, mode=None):
    """Return (fields, meta). Never raises: falls back to rules and says so."""
    mode = (mode or config.EXTRACTOR).lower()
    meta = dict(requested=mode, used=None, notes=list(doc.get("notes", [])))
    use_llm = mode == "llm" or (mode == "auto" and llm_available())
    fields = None
    if use_llm:
        if not llm_available():
            meta["notes"].append("EXTRACTOR=llm but no GEMINI_API_KEY or ANTHROPIC_API_KEY is set - fell back to rules")
            logger.warning("EXTRACTOR=llm but no model API key is set - using rules")
        elif not doc["page_images"] and not doc["text"].strip():
            meta["notes"].append("nothing to send to the model")
        elif not _within_daily_limit():
            meta["notes"].append(f"today's limit of {config.GEMINI_DAILY_LIMIT} documents read by Gemini is used up - read with the built-in rules")
            logger.info("Gemini daily limit reached - using rules")
        else:
            try:
                fields = llm_extract(doc)
                meta["used"] = f"llm:{llm_provider()[1]}"
            except Exception as e:  # network, auth, bad JSON...
                meta["notes"].append(f"LLM extraction failed ({type(e).__name__}: {str(e)[:120]}) - fell back to rules")
                logger.opt(exception=True).warning("LLM extraction failed - using rules")
    if fields is None:
        fields = rules_extract(doc["text"]) if doc["text"].strip() else rules_extract("")
        meta["used"] = "rules"
        if not doc["text"].strip():
            meta["notes"].append("no readable text and no model available - cannot extract")
    fields["grounding"] = ground(fields, doc["text"]) if doc["text"].strip() else {}
    return fields, meta


def ground(fields, text):
    """Does each key value literally appear in the document text? True / False."""
    out = {}
    nums = [to_num(m) for m in re.findall(r"\d[\d,]*\.?\d*", text)]
    nums = [n for n in nums if n is not None]
    if fields.get("total") is not None:
        out["total"] = any(abs(n - fields["total"]) < 0.01 for n in nums)
    if fields.get("subtotal") is not None:  # after a discount the subtotal is worked out, so check the printed figure
        printed = fields["subtotal"] + (fields.get("discount") or 0)
        out["subtotal"] = any(abs(n - printed) < 0.01 for n in nums)
    if fields.get("invoice_number"):
        out["invoice_number"] = norm_alnum(fields["invoice_number"]) in norm_alnum(text)
    if fields.get("invoice_date"):
        out["invoice_date"] = fields["invoice_date"] in all_dates(text)
    if fields.get("vendor_gstin"):
        g = fields["vendor_gstin"].upper()
        toks = re.findall(r"(?<![A-Z0-9])[0-9A-Z]{15}(?![A-Z0-9])", text.upper())
        out["vendor_gstin"] = g in text.upper() or any(fix_gstin(t) == g for t in toks)
    return out
