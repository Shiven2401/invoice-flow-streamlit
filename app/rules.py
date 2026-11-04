"""Procurement rules. Pure functions: easy to read, easy to test, easy to explain.

Every check returns *findings*. A finding has a severity:
    info    - worth showing, never blocks            (e.g. implied PO match, partial billing)
    warn    - worth a second look, doesn't block     (e.g. GSTIN repaired from OCR)
    hold    - a person must look before payment      (e.g. missing date, 6% over PO)
    reject  - do not pay                             (e.g. duplicate, not an invoice, >10% over PO)
Decision = worst severity present. So the reason for a decision is always a
concrete list of findings, never a score nobody can interrogate.
"""
import csv
import difflib
import re
from datetime import date, datetime

from . import config, db
from .config import RULES
from .extract import norm_alnum, parse_date

PAYABLE_TYPES = {"invoice", "bill", "unconfirmed"}
CODE_LABEL = {
    "NOT_AN_INVOICE": "Not an invoice", "INVOICE_UNCONFIRMED": "May not be an invoice", "VENDOR_BLOCKED": "Blocked vendor", "DUPLICATE_EXACT": "Duplicate invoice",
    "OVER_BILLED": "Over the PO by more than 10%", "VENDOR_GSTIN_NAME_MISMATCH": "Vendor identity mismatch",
    "VENDOR_GSTIN_MISMATCH": "GSTIN differs from vendor record", "VENDOR_UNKNOWN": "Vendor not approved",
    "VENDOR_UNIDENTIFIED": "Vendor unreadable", "VENDOR_AMBIGUOUS": "Vendor unclear", "MISSING_FIELD": "Missing invoice details",
    "ARITH_MISMATCH": "Amounts don't add up", "POSSIBLE_DUPLICATE": "Possible duplicate", "INVOICE_NUMBER_REUSED": "Invoice number reused",
    "PO_NOT_FOUND": "PO not found", "PO_VENDOR_MISMATCH": "PO belongs to another vendor", "PO_AMBIGUOUS": "PO unclear",
    "PO_NONE_OPEN": "No open PO", "OVER_TOLERANCE": "Over the PO tolerance", "UNGROUNDED_FIELD": "Value not found in document",
    "GST_RATE_MISMATCH": "GST rate differs from PO", "TAX_BASIS_UNKNOWN": "Tax treatment unclear", "FUTURE_DATE": "Future-dated invoice",
    "CREDIT_NOTE": "Credit note", "CURRENCY": "Foreign currency", "UNREADABLE": "Unreadable document", "PROCESSING_ERROR": "Processing error",
    "USER_INPUT_CONFLICT": "Typed value differs from document", "USER_ENTERED": "Value typed in at upload",
    "PO_NOT_NEEDED": "Vendor doesn't use POs", "PO_ASSIGNED": "PO chosen by a person", "PO_REF_ACCEPTED": "PO number accepted earlier", "VENDOR_ADDED": "Vendor approved for this folder", "PO_APPROVED": "PO approved for this upload",
}
# Analytics: fraud and control flags, kept apart from the general holds: the first group needs a phone call to the vendor,
# the second only a corrected copy. Counted at any severity, so a warning still shows up
FLAG_KIND = {"call": ["VENDOR_GSTIN_MISMATCH", "VENDOR_GSTIN_NAME_MISMATCH", "VENDOR_BLOCKED", "PO_VENDOR_MISMATCH", "DUPLICATE_EXACT",
                      "INVOICE_NUMBER_REUSED", "POSSIBLE_DUPLICATE", "OVER_BILLED", "USER_INPUT_CONFLICT"],
             "resend": ["GSTIN_FORMAT", "VENDOR_NO_GSTIN", "TAX_BASIS_UNKNOWN", "GST_RATE_MISMATCH", "TAX_RATE_ASSUMED", "ARITH_MISMATCH",
                        "LINE_MATH", "UNGROUNDED_FIELD", "MISSING_FIELD", "FUTURE_DATE"]}

PO_HOLDS = {"PO_NOT_FOUND", "PO_AMBIGUOUS", "PO_NONE_OPEN"}   # a person can answer these on the Decision page
NO_PO_WAIVES = {"PO_AMBIGUOUS", "PO_NONE_OPEN"}               # only a bill that cites no PO can go without one


def F(code, severity, stage, message, **extra):
    return dict(code=code, severity=severity, stage=stage, message=message, **extra)


def inr(x):
    """Rs 1,18,000.00 (Indian digit grouping) for human-readable messages."""
    if x is None:
        return "-"
    whole, dec = f"{abs(x):.2f}".split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        whole = head + "," + tail
    return f"{'-' if x < 0 else ''}Rs {whole}.{dec}"


def tol(x):
    return max(RULES["arith_abs_tol"], abs(x or 0) * RULES["arith_rel_tol"])


# ------------------------------------------------------------ master data --

def load_vendors(user_id):
    """The shared master list, plus the vendors this account has approved."""
    out = []
    with open(config.DATA_DIR / "vendors.csv", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            r["aliases"] = [a.strip() for a in (r.get("aliases") or "").split("|") if a.strip()]
            r["source"] = "master list"
            out.append(r)
    for v in db.added_vendors(user_id):
        out.append(dict(v, aliases=[a for a in (v["aliases"] or "").split("|") if a], source="approved in app"))
    learned = db.vendor_aliases(user_id)  # names learned from bills a person approved
    for v in out:
        v["aliases"] = v["aliases"] + [a for a in learned.get(v["vendor_id"], []) if a not in v["aliases"]]
    return out


def load_pos():
    out = []
    with open(config.DATA_DIR / "purchase_orders.csv", newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            r["net_amount"] = float(r["net_amount"])
            r["gst_rate"] = float(r["gst_rate"])
            out.append(r)
    return out


# ----------------------------------------------------------------- vendor --

_SUFFIX = {"pvt", "private", "ltd", "limited", "llp", "inc", "co", "company", "corp", "corporation", "and", "the", "m", "s", "ms"}
GSTIN_RE = re.compile(r"^\d{2}[A-Z]{5}\d{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")


def norm_name(s):
    s = (s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return " ".join(t for t in s.split() if t not in _SUFFIX)


def name_score(a, b):
    na, nb = norm_name(a), norm_name(b)
    if not na or not nb:
        return 0.0
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    jacc = len(ta & tb) / len(ta | tb)
    contain = len(ta & tb) / min(len(ta), len(tb))
    return round(max(ratio, jacc, 0.9 * contain), 3)


def best_vendor_scores(name, vendors):
    out = []
    for v in vendors:
        s = max([name_score(name, v["name"])] + [name_score(name, a) for a in v["aliases"]])
        out.append((s, v))
    return sorted(out, key=lambda x: -x[0])


def match_vendor(f, vendors):
    """Identify the vendor from GSTIN first (strong), name second. Returns (result, findings)."""
    name, gstin = f.get("vendor_name"), (f.get("vendor_gstin") or "").upper() or None
    res = dict(status="unknown", vendor=None, method=None, name_score=0.0, candidates=[],
               gstin=gstin, gstin_valid=bool(gstin and GSTIN_RE.match(gstin)))
    fs = []
    scored = best_vendor_scores(name, vendors) if name else []
    res["candidates"] = [dict(vendor_id=v["vendor_id"], name=v["name"], score=s) for s, v in scored[:3]]
    by_gstin = next((v for v in vendors if gstin and v["gstin"].upper() == gstin), None)
    top_s, top_v = scored[0] if scored else (0.0, None)

    if gstin and not res["gstin_valid"]:
        fs.append(F("GSTIN_FORMAT", "warn", "vendor", f"GSTIN '{gstin}' is not a valid 15-character GSTIN format - verify against the document."))

    if by_gstin:
        s = max([name_score(name, by_gstin["name"])] + [name_score(name, a) for a in by_gstin["aliases"]]) if name else 1.0
        res.update(vendor=by_gstin, name_score=s, method="GSTIN" + (" + name" if name else ""))
        if name and s < RULES["vendor_name_weak"]:
            res["status"] = "gstin_name_mismatch"
            fs.append(F("VENDOR_GSTIN_NAME_MISMATCH", "hold", "vendor",
                        f"GSTIN {gstin} belongs to '{by_gstin['name']}' but the invoice is issued in the name '{name}'. Possible spoofed or mis-keyed vendor identity."))
            return res, fs
    elif top_v and top_s >= RULES["vendor_name_strong"]:
        res.update(vendor=top_v, name_score=top_s, method="name")
        if gstin and gstin != top_v["gstin"].upper():
            res["status"] = "gstin_mismatch"
            fs.append(F("VENDOR_GSTIN_MISMATCH", "hold", "vendor",
                        f"Name matches '{top_v['name']}' but GSTIN {gstin} differs from the one on file ({top_v['gstin']}). Verify before paying - a changed tax ID is a classic fraud signal."))
            return res, fs
        if not gstin:
            fs.append(F("VENDOR_NO_GSTIN", "warn", "vendor", f"Matched '{top_v['name']}' by name only - no GSTIN on the invoice to confirm."))
    elif top_v and top_s >= RULES["vendor_name_weak"] and not res["gstin_valid"]:  # its own valid GSTIN makes it a different vendor
        res.update(status="ambiguous", name_score=top_s)
        fs.append(F("VENDOR_AMBIGUOUS", "hold", "vendor",
                    f"'{name}' only loosely resembles approved vendor '{top_v['name']}' (similarity {top_s:.0%}) and no GSTIN confirms it."))
        return res, fs
    else:
        if not name and not gstin:
            res["status"] = "no_identity"
            fs.append(F("VENDOR_UNIDENTIFIED", "hold", "vendor", "Could not read a vendor name or GSTIN from the document."))
        else:
            fs.append(F("VENDOR_UNKNOWN", "hold", "vendor",
                        f"'{name or gstin}' is not on the approved vendor list" + (f" (GSTIN {gstin})." if gstin else ".")))
        return res, fs

    v = res["vendor"]
    if v["status"].lower() != "approved":
        res["status"] = "blocked"
        fs.append(F("VENDOR_BLOCKED", "reject", "vendor", f"Vendor '{v['name']}' is {v['status']} - no payments allowed."))
    else:
        res["status"] = "approved"
    return res, fs


def vendor_approval_problem(name, gstin, vendors):
    """Why this vendor can't be added to the approved list, or None. Approval only ever adds a
    new identity: it must not become a way round a blocked vendor or a vendor already on file."""
    if not name:
        return "A vendor name is needed."
    if gstin and not GSTIN_RE.match(gstin):
        return f"'{gstin}' is not a valid 15-character GSTIN."
    same = next((v for v in vendors if gstin and v["gstin"].upper() == gstin), None)
    if same:
        return f"GSTIN {gstin} already belongs to {same['name']} ({same['status']})."
    score, closest = best_vendor_scores(name, vendors)[0]
    # with a GSTIN of its own a loose resemblance is just a shared word ("Office"); without one, or close to a blocked
    # vendor, or nearly the same name, it could be posing as that vendor
    distinct = gstin and closest["status"].lower() == "approved" and score < RULES["vendor_name_strong"]
    if score >= RULES["vendor_name_weak"] and not distinct:
        return f"'{name}' looks like existing vendor '{closest['name']}' ({closest['status']}), so it can't be added as a new one."
    return None


def next_vendor_id(vendors):
    return f"V{max(int(v['vendor_id'][1:]) for v in vendors) + 1:03d}"


# ---------------------------------------------------------------- amounts --

def derive_amounts(f, assumed_rate=None):
    """Work out the PRE-TAX amount to compare with the PO, whether tax is separate or embedded."""
    sub, tax, total, rate, incl = f.get("subtotal"), f.get("tax_amount"), f.get("total"), f.get("tax_rate"), f.get("tax_included")
    items = [li["amount"] for li in f.get("line_items", []) if li.get("amount") is not None]
    d = dict(net=None, tax=None, gross=total, rate=rate, basis=None, assumed_rate=False, lines_sum=round(sum(items), 2) if items else None)
    if sub is not None:
        d.update(net=sub, basis="stated subtotal", tax=tax if tax is not None else (round(total - sub, 2) if total is not None else None))
    elif tax is not None and total is not None:
        d.update(net=round(total - tax, 2), tax=tax, basis="total minus stated tax")
    elif incl and total is not None:
        r = rate
        if r is None and assumed_rate is not None:
            r, d["assumed_rate"] = assumed_rate, True
        if r is not None:
            net = round(total / (1 + r / 100), 2)
            d.update(net=net, tax=round(total - net, 2), rate=r, basis=f"GST embedded: total / (1 + {r:g}%)")
        else:
            d.update(net=total, basis="total (tax treatment unknown)")
    elif total is not None:
        d.update(net=total, basis="total (tax treatment unknown)")
    elif d["lines_sum"]:
        d.update(net=d["lines_sum"], basis="sum of line items")
    if d["rate"] is None and d["net"] and d["tax"] is not None and d["net"] > 0:
        d["rate"] = round(d["tax"] / d["net"] * 100, 2)
    return d


def validate(f, d):
    """Completeness + arithmetic + sanity. Returns (checks, findings)."""
    checks, fs = [], []
    dt = f.get("doc_type")
    if dt not in PAYABLE_TYPES:
        if dt == "credit_note":
            fs.append(F("CREDIT_NOTE", "hold", "validate", "This is a credit note, not an invoice - route to AP for manual netting against the original invoice."))
        elif dt in ("quotation", "proforma", "receipt"):
            label = {"quotation": "a quotation", "proforma": "a pro-forma", "receipt": "a payment receipt/confirmation"}[dt]
            fs.append(F("NOT_AN_INVOICE", "reject", "validate", f"Document is {label}, not a payable tax invoice."))
        else:
            fs.append(F("NOT_AN_INVOICE", "reject", "validate", "This document is not an invoice: it has no invoice title and too few invoice details (number, date, amounts)."))
    if dt == "unconfirmed":
        fs.append(F("INVOICE_UNCONFIRMED", "hold", "validate", "This may not be an invoice: it has some invoice details but no invoice title. Check the document before approving."))
    missing = [k for k in RULES["critical_fields"] if not f.get(k)]
    label = dict(vendor_name="vendor name", invoice_number="invoice number", invoice_date="invoice date", total="total amount")
    checks.append(dict(name="Critical fields present", ok=not missing,
                       detail="all of vendor, invoice number, date, total found" if not missing else "missing: " + ", ".join(label[m] for m in missing)))
    if missing and dt in PAYABLE_TYPES:
        fs.append(F("MISSING_FIELD", "hold", "validate", "Missing " + ", ".join(label[m] for m in missing) + " - cannot be safely processed or de-duplicated.", fields=missing))

    ungrounded = [k for k, v in (f.get("grounding") or {}).items() if v is False and k in ("total", "invoice_number", "invoice_date", "subtotal")]
    if ungrounded:
        fs.append(F("UNGROUNDED_FIELD", "hold", "validate",
                    "Extracted value(s) not found in the document text: " + ", ".join(ungrounded) + " - possible misread.", fields=ungrounded))
    checks.append(dict(name="Extracted values found in the document text", ok=not ungrounded,
                       detail="every key value is literally present in the source" if not ungrounded else "not found: " + ", ".join(ungrounded)))

    sub, tax, total = f.get("subtotal"), f.get("tax_amount"), f.get("total")
    ls = d["lines_sum"]
    if ls is not None and not f.get("items_incomplete"):  # a row OCR couldn't read is not an arithmetic error on the invoice
        if sub is not None and f.get("discount"):
            target, what = round(sub + f["discount"], 2), "amount before discount"
        elif sub is not None:
            target, what = sub, "stated subtotal"
        elif f.get("tax_included") and total is not None:
            target, what = total, "total (GST embedded)"
        elif total is not None and tax is not None:
            target, what = round(total - tax, 2), "total minus tax"
        else:
            target, what = None, None
        if target is not None:
            ok = abs(ls - target) <= tol(target)
            checks.append(dict(name="Line items add up", ok=ok, detail=f"lines {inr(ls)} vs {what} {inr(target)}"
                               + ("" if ok else f" (off by {inr(abs(ls - target))})")))
            if not ok:
                fs.append(F("ARITH_MISMATCH", "hold", "validate",
                            f"Line items add up to {inr(ls)} but the invoice states {inr(target)} ({what}) - off by {inr(abs(ls - target))}."))
    if sub is not None and tax is not None and total is not None:
        diff = abs(sub + tax - total)
        ok = diff <= tol(total)
        checks.append(dict(name="Subtotal + tax = total", ok=ok, detail=f"{inr(sub)} + {inr(tax)} = {inr(sub + tax)} vs total {inr(total)}"
                           + (" (rounding)" if ok and diff > 0.005 else "")))
        if not ok:
            fs.append(F("ARITH_MISMATCH", "hold", "validate", f"Subtotal {inr(sub)} + tax {inr(tax)} = {inr(sub + tax)}, but the invoice total is {inr(total)}."))
        elif diff > 0.005:
            fs.append(F("ROUNDING", "info", "validate", f"Rounding difference of {inr(diff)} between components and total - within tolerance."))
    rate = f.get("tax_rate")
    if sub is not None and tax is not None and rate:
        exp = round(sub * rate / 100, 2)
        ok = abs(exp - tax) <= tol(tax)
        checks.append(dict(name="Tax matches stated rate", ok=ok, detail=f"{rate:g}% of {inr(sub)} = {inr(exp)} vs stated {inr(tax)}"))
        if not ok:
            fs.append(F("ARITH_MISMATCH", "hold", "validate", f"Tax of {inr(tax)} does not equal {rate:g}% of {inr(sub)} ({inr(exp)})."))
    bad = [li for li in f.get("line_items", []) if li.get("quantity") and li.get("unit_price") and li.get("amount") is not None
           and abs(li["quantity"] * li["unit_price"] - li["amount"]) > max(1.0, 0.005 * li["amount"])]
    if f.get("line_items") and any(li.get("quantity") and li.get("unit_price") for li in f["line_items"]):
        checks.append(dict(name="Quantity x rate = line amount", ok=not bad, detail="every line multiplies out" if not bad else f"{len(bad)} line(s) don't multiply out"))
        if bad:
            fs.append(F("LINE_MATH", "warn", "validate", f"{len(bad)} line item(s) where quantity x rate does not equal the line amount."))
    if d["basis"] and "unknown" in d["basis"] and dt in PAYABLE_TYPES and total is not None:
        fs.append(F("TAX_BASIS_UNKNOWN", "hold", "validate", "Only a total is shown - cannot tell whether GST is included, so the pre-tax amount can't be compared with the PO."))
    if d.get("assumed_rate"):
        fs.append(F("TAX_RATE_ASSUMED", "warn", "validate", f"GST is embedded but no rate is printed; assumed the PO's rate ({d['rate']:g}%)."))
    elif d["basis"] and d["basis"].startswith("GST embedded"):
        fs.append(F("TAX_EMBEDDED", "info", "validate", f"GST is embedded in the amount - backed out {d['rate']:g}% to get a pre-tax value of {inr(d['net'])} (tax {inr(d['tax'])})."))

    cur = (f.get("currency") or "INR").upper()
    if cur not in ("INR", "RS", "RS.", "₹"):
        fs.append(F("CURRENCY", "hold", "validate", f"Invoice currency is {cur}; this process is configured for INR only."))

    inv_date = parse_date(f.get("invoice_date"))
    if inv_date:
        age = (date.fromisoformat(config.AS_OF) - date.fromisoformat(inv_date)).days
        checks.append(dict(name="Invoice date is sensible", ok=0 <= age <= RULES["stale_days"], detail=f"{inv_date} ({age} days before {config.AS_OF})"))
        if age < 0:
            fs.append(F("FUTURE_DATE", "hold", "validate", f"Invoice is dated {inv_date}, which is in the future."))
        elif age > RULES["stale_days"]:
            fs.append(F("STALE_INVOICE", "warn", "validate", f"Invoice is {age} days old - check it hasn't already been paid by another route."))
    return checks, fs


# ---------------------------------------------------------------- PO match --

_STOP = {"the", "and", "for", "per", "with", "from", "order", "dated", "date", "units", "unit", "nos", "pcs", "qty", "each",
         "supplied", "against", "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"}


def _tokens(s):
    out = set()
    for w in re.findall(r"[a-z]{3,}", (s or "").lower()):
        if w in _STOP:
            continue
        out.add(w[:-1] if len(w) > 4 and w.endswith("s") else w)
    return out


def _closeness(a, b):
    return max(0.0, 1 - abs(a - b) / b) if b else 0.0


def match_po(f, d, vendor, pos, billed_fn):
    """Explicit PO reference first. Otherwise infer from vendor + amount + description + date."""
    ref = f.get("po_reference")
    out = dict(status="none", po=None, reference=ref, candidates=[])
    fs = []
    by_num = {norm_alnum(p["po_number"]): p for p in pos}
    if ref and norm_alnum(ref) in by_num:
        po = by_num[norm_alnum(ref)]
        out.update(status="explicit", po=po)
        if vendor and po["vendor_id"] != vendor["vendor_id"]:
            owner = next((p for p in pos if p["po_number"] == po["po_number"]), po)["vendor_id"]
            out["status"] = "vendor_mismatch"
            fs.append(F("PO_VENDOR_MISMATCH", "hold", "po", f"{po['po_number']} was issued to vendor {owner}, not to {vendor['name']}."))
        else:
            fs.append(F("PO_EXPLICIT", "info", "po", f"Invoice cites {po['po_number']} ({po['description']})."))
        return out, fs

    cands = []
    if vendor:
        inv_tokens = _tokens(f.get("description_text"))
        for p in pos:
            if p["vendor_id"] != vendor["vendor_id"] or p["status"] != "open":
                continue
            remaining = p["net_amount"] - billed_fn(p["po_number"])
            if remaining <= 0.005:
                continue
            net = d["net"] if d["net"] is not None else 0
            amt = max(_closeness(net, remaining), _closeness(net, p["net_amount"]))
            pt = _tokens(p["description"])
            desc = len(pt & inv_tokens) / min(len(pt), len(inv_tokens)) if pt and inv_tokens else 0.0
            inv_date = parse_date(f.get("invoice_date"))
            dscore = 0.5 if not inv_date else (1.0 if inv_date >= p["po_date"] else 0.0)
            cands.append(dict(po_number=p["po_number"], description=p["description"], remaining=round(remaining, 2),
                              amount_score=round(amt, 2), desc_score=round(desc, 2),
                              score=round(0.6 * amt + 0.3 * desc + 0.1 * dscore, 3)))
        cands.sort(key=lambda c: -c["score"])
    out["candidates"] = cands[:3]

    if ref:  # a reference was printed but doesn't exist
        out["status"] = "ref_not_found"
        hint = f" Closest open PO for this vendor: {cands[0]['po_number']}." if cands else ""
        fs.append(F("PO_NOT_FOUND", "hold", "po", f"Invoice cites '{ref}', which does not exist in the PO register.{hint}"))
        return out, fs
    if not vendor:
        fs.append(F("PO_NOT_CHECKED", "info", "po", "No PO matching attempted - vendor is not identified."))
        return out, fs
    if cands and cands[0]["score"] >= RULES["implied_po_min_score"] and (
            len(cands) == 1 or cands[0]["score"] - cands[1]["score"] >= RULES["implied_po_min_margin"]):
        top = cands[0]
        po = next(p for p in pos if p["po_number"] == top["po_number"])
        out.update(status="implied", po=po)
        fs.append(F("IMPLIED_PO_MATCH", "info", "po",
                    f"No PO number on the invoice. Matched {po['po_number']} ({po['description']}) with confidence {top['score']:.0%}: "
                    f"amount fit {top['amount_score']:.0%}, description overlap {top['desc_score']:.0%}, unique best candidate."))
        return out, fs
    out["status"] = "ambiguous" if cands else "none"
    if cands:
        fs.append(F("PO_AMBIGUOUS", "hold", "po", "No PO number on the invoice and no open PO is a confident match (best: "
                    + ", ".join(f"{c['po_number']} {c['score']:.0%}" for c in cands[:2]) + ")."))
    else:
        fs.append(F("PO_NONE_OPEN", "hold", "po", f"No PO number on the invoice and {vendor['name']} has no open PO with a balance."))
    return out, fs


# ------------------------------------------------------------- tolerance ---

def budget_check(po, net, billed_before, invoice_rate):
    """CUMULATIVE check: this invoice + everything already approved against the PO, vs the PO value."""
    po_net = po["net_amount"]
    cum = round(billed_before + net, 2)
    over = cum - po_net
    over_pct = over / po_net * 100
    res = dict(po_number=po["po_number"], po_net=po_net, billed_before=round(billed_before, 2), this_net=net, cumulative=cum,
               remaining_after=round(max(0.0, po_net - cum), 2), over_amount=round(max(0.0, over), 2),
               over_pct=round(max(0.0, over_pct), 2), pct_billed=round(cum / po_net * 100, 1),
               tolerance_pct=RULES["tolerance_pct"], hold_band_pct=RULES["hold_band_pct"], band="within")
    fs = []
    if over <= tol(po_net):
        if abs(over) <= tol(po_net) and cum >= po_net - tol(po_net):
            res["band"] = "exact"
            fs.append(F("PO_FULLY_BILLED", "info", "budget", f"With this invoice, cumulative billing is {inr(cum)}, equal to the PO value {inr(po_net)} - the PO is fully billed."))
        else:
            res["band"] = "partial"
            fs.append(F("PARTIAL_BILLING", "info", "budget",
                        f"Partial billing: with this invoice {res['pct_billed']:g}% of the PO value {inr(po_net)} is billed, {inr(res['remaining_after'])} still open."))
    elif over_pct <= RULES["tolerance_pct"]:
        res["band"] = "within_tolerance"
        fs.append(F("WITHIN_TOLERANCE", "info", "budget",
                    f"Cumulative billing {inr(cum)} is {inr(over)} ({over_pct:.1f}%) above the PO value {inr(po_net)} - inside the {RULES['tolerance_pct']:g}% tolerance."))
    elif over_pct <= RULES["hold_band_pct"]:
        res["band"] = "hold"
        fs.append(F("OVER_TOLERANCE", "hold", "budget",
                    f"Cumulative billing {inr(cum)} is {inr(over)} ({over_pct:.1f}%) above the PO value {inr(po_net)} - outside the {RULES['tolerance_pct']:g}% tolerance"
                    + (f" (this invoice {inr(net)} on top of {inr(billed_before)} already approved)." if billed_before else ".")))
    else:
        res["band"] = "reject"
        fs.append(F("OVER_BILLED", "reject", "budget",
                    f"Cumulative billing {inr(cum)} is {over_pct:.0f}% above the PO value {inr(po_net)} - beyond the {RULES['hold_band_pct']:g}% review band."))
    if invoice_rate is not None and abs(invoice_rate - po["gst_rate"]) > RULES["gst_rate_tol_pp"]:
        fs.append(F("GST_RATE_MISMATCH", "hold", "budget", f"Invoice GST rate {invoice_rate:g}% differs from the {po['gst_rate']:g}% on the PO."))
    return res, fs


# -------------------------------------------------------------- duplicates --

def duplicate_check(f, vendor, file_hash, priors, hash_prior):
    fs, matches = [], []
    if hash_prior:
        matches.append(dict(run_id=hash_prior["id"], filename=hash_prior["filename"], reason="identical file"))
        fs.append(F("DUPLICATE_EXACT", "reject", "duplicates", f"This exact file was already processed (run {hash_prior['id']}, {hash_prior['filename']})."))
    my_v = vendor["vendor_id"] if vendor else norm_name(f.get("vendor_name"))
    inv_n = norm_alnum(f.get("invoice_number"))
    for p in priors:
        if hash_prior and p["id"] == hash_prior["id"]:
            continue
        pv = p["vendor_id"] or norm_name(p["vendor_name"])
        if not my_v or pv != my_v:
            continue
        same_no = bool(inv_n) and norm_alnum(p["invoice_number"]) == inv_n
        same_amt = f.get("total") is not None and p["total"] is not None and abs(p["total"] - f["total"]) <= RULES["arith_abs_tol"]
        same_date = bool(f.get("invoice_date")) and f["invoice_date"] == p["invoice_date"]
        if same_no and same_amt:
            matches.append(dict(run_id=p["id"], filename=p["filename"], reason="same vendor + invoice number + amount"))
            fs.append(F("DUPLICATE_EXACT", "reject", "duplicates",
                        f"Same vendor, invoice number {f['invoice_number']} and amount as an earlier submission (run {p['id']}, {p['filename']}) - paying it again would double-pay."))
        elif same_no:
            matches.append(dict(run_id=p["id"], filename=p["filename"], reason="invoice number reused with a different amount"))
            fs.append(F("INVOICE_NUMBER_REUSED", "hold", "duplicates",
                        f"Invoice number {f['invoice_number']} was already used by this vendor ({inr(p['total'])}) but this one is {inr(f['total'])}."))
        elif same_amt and same_date:
            matches.append(dict(run_id=p["id"], filename=p["filename"], reason="same vendor + amount + date, different/missing number"))
            fs.append(F("POSSIBLE_DUPLICATE", "hold", "duplicates",
                        f"Same vendor, amount ({inr(f['total'])}) and date ({f['invoice_date']}) as an earlier invoice ({p['invoice_number'] or 'no number'}, run {p['id']}) "
                        f"but a {'missing' if not inv_n else 'different'} invoice number - possible resubmission."))
    return dict(matches=matches, checked_against=len(priors)), fs


# ------------------------------------------------------- decision + wording --

SEV = {"info": 0, "warn": 1, "hold": 2, "reject": 3}
PRIORITY = ["NOT_AN_INVOICE", "VENDOR_BLOCKED", "DUPLICATE_EXACT", "OVER_BILLED", "VENDOR_GSTIN_NAME_MISMATCH", "VENDOR_GSTIN_MISMATCH",
            "VENDOR_UNKNOWN", "VENDOR_UNIDENTIFIED", "VENDOR_AMBIGUOUS", "USER_INPUT_CONFLICT", "INVOICE_UNCONFIRMED", "MISSING_FIELD", "ARITH_MISMATCH", "POSSIBLE_DUPLICATE",
            "INVOICE_NUMBER_REUSED", "PO_NOT_FOUND", "PO_VENDOR_MISMATCH", "PO_AMBIGUOUS", "PO_NONE_OPEN", "OVER_TOLERANCE"]

NEXT = {
    "NOT_AN_INVOICE": "No payment action. File or return the document - it is not a payable invoice.",
    "VENDOR_BLOCKED": "Do not pay. Escalate to procurement - this vendor is blocked.",
    "DUPLICATE_EXACT": "Do not pay. Link to the original and notify the vendor that it was already received.",
    "OVER_BILLED": "Do not pay. Ask procurement to confirm whether the PO needs amending, then request a corrected invoice or credit note.",
    "VENDOR_GSTIN_NAME_MISMATCH": "Verify the vendor's identity with procurement before any payment.",
    "VENDOR_GSTIN_MISMATCH": "Verify the GSTIN with the vendor through a known contact before any payment.",
    "VENDOR_UNKNOWN": "Verify this vendor, then approve it below to add it to the approved list and re-check the invoice.",
    "VENDOR_UNIDENTIFIED": "Ask the sender for a legible copy showing the vendor name and GSTIN.",
    "VENDOR_AMBIGUOUS": "Procurement to confirm which approved vendor this is.",
    "USER_INPUT_CONFLICT": "Compare the typed value with the original document before deciding.",
    "INVOICE_UNCONFIRMED": "Open the original document and confirm it is a bill before approving it.",
    "MISSING_FIELD": "Ask the vendor to resend with the missing information (draft below). Do not pay until received.",
    "ARITH_MISMATCH": "Ask the vendor to correct and reissue the invoice (draft below).",
    "POSSIBLE_DUPLICATE": "Confirm with the vendor whether this is a resubmission before paying (draft below).",
    "INVOICE_NUMBER_REUSED": "Ask the vendor to explain the reused invoice number (draft below).",
    "PO_NOT_FOUND": "Ask the vendor for the correct PO number (draft below).",
    "PO_VENDOR_MISMATCH": "Ask the vendor to confirm the PO number (draft below).",
    "PO_AMBIGUOUS": "Procurement to assign the correct PO, or ask the vendor for the PO number.",
    "PO_NONE_OPEN": "Procurement to confirm whether a PO exists or needs raising.",
    "OVER_TOLERANCE": "Procurement to approve the variance or have the vendor reissue within the PO value.",
    "UNGROUNDED_FIELD": "A person should check the extracted values against the document image.",
    "GST_RATE_MISMATCH": "Ask the vendor to confirm the GST rate (draft below).",
    "TAX_BASIS_UNKNOWN": "Ask the vendor to resend with GST shown separately (draft below).",
    "FUTURE_DATE": "Ask the vendor to correct the invoice date (draft below).",
    "UNREADABLE": "Ask the sender for a clearer copy, or key the invoice in manually.",
    "CREDIT_NOTE": "AP to net this against the original invoice manually.",
    "CURRENCY": "AP to handle foreign-currency invoices manually.",
}
VENDOR_FIXABLE = {"MISSING_FIELD", "ARITH_MISMATCH", "POSSIBLE_DUPLICATE", "INVOICE_NUMBER_REUSED", "PO_NOT_FOUND",
                  "PO_VENDOR_MISMATCH", "GST_RATE_MISMATCH", "TAX_BASIS_UNKNOWN", "FUTURE_DATE", "PO_AMBIGUOUS"}


def decide(findings):
    sev = {x["severity"] for x in findings}
    return "REJECT" if "reject" in sev else "HOLD" if "hold" in sev else "APPROVE"


def explain(decision, findings, f, d, po_res, budget):
    blocking = [x for x in findings if x["severity"] in ("hold", "reject")]
    blocking.sort(key=lambda x: (PRIORITY.index(x["code"]) if x["code"] in PRIORITY else 99))
    po = (po_res or {}).get("po")
    if decision == "APPROVE":
        headline = f"Approved for payment - {inr(f.get('total'))} gross" + (f" against {po['po_number']}" if po else "") + "."
        next_steps = ["Post to the ledger and schedule payment per terms."]
        if any(x["code"] in ("IMPLIED_PO_MATCH", "TAX_RATE_ASSUMED", "VENDOR_NO_GSTIN") for x in findings):
            next_steps.append("Worth a glance at the notes above: part of this match was inferred rather than printed on the invoice.")
    elif decision == "REJECT":
        headline = "Rejected - " + (blocking[0]["message"] if blocking else "see findings")
        next_steps = []
        for x in blocking:
            n = NEXT.get(x["code"])
            if n and n not in next_steps:
                next_steps.append(n)
    else:
        n = len(blocking)
        headline = f"Held for review - {n} issue{'s' if n != 1 else ''}: " + "; ".join(x["message"].rstrip(".") for x in blocking[:2]) + ("; ..." if n > 2 else "") + "."
        next_steps = []
        for x in blocking:
            nx = NEXT.get(x["code"])
            if nx and nx not in next_steps:
                next_steps.append(nx)
    return dict(headline=headline, next_steps=next_steps)


def draft_message(decision, f, vendor, findings):
    """A ready-to-send query for the vendor (or an internal note) when a person is needed."""
    if decision == "APPROVE":
        return None
    blocking = [x for x in findings if x["severity"] in ("hold", "reject")]
    codes = {x["code"] for x in blocking}
    vname = (vendor or {}).get("name") or f.get("vendor_name") or "Sir/Madam"
    ref = f.get("invoice_number") or (f"dated {f['invoice_date']}" if f.get("invoice_date") else "(no number)")
    if codes & VENDOR_FIXABLE and not (codes & {"DUPLICATE_EXACT", "VENDOR_BLOCKED", "NOT_AN_INVOICE", "OVER_BILLED"}):
        asks = []
        for x in blocking:
            if x["code"] == "MISSING_FIELD":
                asks += [f"the {m.replace('_', ' ')}" for m in x.get("fields", [])]
            elif x["code"] in VENDOR_FIXABLE:
                asks.append(NEXT[x["code"]].split(" (draft")[0].replace("Ask the vendor to ", "").rstrip("."))
        def line(x):
            if x["code"] == "MISSING_FIELD":
                return "Your invoice does not show the " + ", ".join(m.replace("_", " ") for m in x.get("fields", [])) + "."
            return x["message"]
        body = "\n".join(f"  - {line(x)}" for x in blocking if x["code"] in VENDOR_FIXABLE)
        return dict(audience="vendor", subject=f"Invoice {ref} - clarification needed before we can process payment",
                    body=f"Dear {vname},\n\nThank you for your invoice {ref}. We are unable to process it yet because:\n\n{body}\n\n"
                         f"Please reply with a corrected invoice or the missing information so we can schedule payment.\n\nRegards,\nAccounts Payable, {config.BUYER_NAME}")
    reason = "\n".join(f"  - {x['message']}" for x in blocking)
    past = {"HOLD": "held", "REJECT": "rejected"}[decision]
    return dict(audience="procurement", subject=f"Invoice {ref} from {vname} - {past} - your input needed",
                body=f"Hi Procurement team,\n\nInvoice {ref} from {vname} was {past} by the AP automation:\n\n{reason}\n\n"
                     f"Please advise on the next step.\n\nThanks,\nAP automation")
