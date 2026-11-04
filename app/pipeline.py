"""The invoice pipeline: 9 visible stages from raw file to explained decision.

    ingest -> read -> extract -> validate -> vendor -> po -> duplicates -> budget -> decision

`run()` calls emit(stage, status, title, summary, detail) as each stage starts and
finishes - that is what the live run view renders. A stage can finish ok / warn /
fail, or be skipped (with a reason) when an earlier finding makes it pointless.
"""
import json
import time
from pathlib import Path

import pymupdf
from loguru import logger

from . import config, db, extract, rules
from .rules import inr

STAGES = [
    ("ingest", "Receive document"),
    ("read", "Read document"),
    ("extract", "Extract fields"),
    ("validate", "Validate & reconcile"),
    ("vendor", "Verify vendor"),
    ("po", "Match purchase order"),
    ("duplicates", "Duplicate check"),
    ("budget", "PO balance & tolerance"),
    ("decision", "Decision"),
]
TITLES = dict(STAGES)


def _status(fs):
    sev = {x["severity"] for x in fs}
    return "fail" if "reject" in sev else "warn" if sev & {"hold", "warn"} else "ok"


def _public_fields(f):
    keys = ["doc_type", "vendor_name", "vendor_gstin", "buyer_name", "invoice_number", "invoice_date", "po_reference",
            "currency", "discount", "subtotal", "tax_amount", "tax_rate", "tax_included", "total"]
    return {k: f.get(k) for k in keys}


def _stages(emit, findings):
    """How a stage reports: started, ended with findings, or skipped. Findings collect as stages end."""
    def start(sid):
        emit(sid, "running", TITLES[sid], "", {})
        if config.STAGE_DELAY:
            time.sleep(config.STAGE_DELAY)

    def end(sid, fs, summary, detail, status=None):
        findings.extend(fs)
        status = status or _status(fs)
        logger.debug("{} {}: {}", sid, status, summary)
        emit(sid, status, TITLES[sid], summary, detail)

    def skip(sid, why):
        logger.debug("{} skipped: {}", sid, why)
        emit(sid, "skipped", TITLES[sid], why, {"reason": why})

    return start, end, skip


def _apply_inputs(f, inputs):
    """Put what a person supplied into the extracted fields. Returns the findings that keep it visible."""
    if inputs.get("is_invoice") and f.get("doc_type") not in rules.PAYABLE_TYPES | {"credit_note"}:
        f["doc_type"] = "unconfirmed"  # a person overruled "not an invoice": check it, but still hold it for them
    entered, conflicts = extract.apply_inputs(f, inputs)
    fs = [rules.F("USER_INPUT_CONFLICT", "hold", "extract", f"{label}: the document reads '{read}' but '{typed}' was typed in.")
          for label, read, typed in conflicts]
    if inputs.get("batch"):  # answers given on a folder's first invoice: {id, vendor, po, all_vendors}
        f["_batch"] = inputs["batch"]
    if inputs.get("po_assigned"):  # chosen on the Decision page from the vendor's open POs, so it isn't a typed contradiction
        read = f.get("po_reference")
        f["po_reference"] = inputs["po_assigned"]
        fs.append(rules.F("PO_ASSIGNED", "info", "po", f"A person matched this invoice to {inputs['po_assigned']}"
                          + (f" (the document cites '{read}')." if read and read != inputs["po_assigned"] else ".")))
    if entered:
        fs.append(rules.F("USER_ENTERED", "warn", "extract", f"Typed in, not read from the document: {', '.join(entered)}."))
    return fs


def _end_extract(end, f, meta, input_fs, run_id):
    found = [k for k in ("vendor_name", "invoice_number", "invoice_date", "po_reference", "total") if f.get(k)]
    # whether the vendor is already approved, so the Review page can offer to approve a new one before the checks,
    # and whether the PO it cites is in the register, so a folder's first invoice can settle the PO question for all
    vres, _ = rules.match_vendor(f, rules.load_vendors(db.run_owner(run_id)))
    ref = extract.norm_alnum(f.get("po_reference"))
    po_check = dict(reference=f.get("po_reference"), known=bool(ref) and any(extract.norm_alnum(p["po_number"]) == ref for p in rules.load_pos()))
    end("extract", [rules.F("EXTRACT_NOTE", "warn", "extract", n) for n in meta["notes"] if "fell back" in n or "cannot extract" in n] + input_fs,
        f"{meta['used']} - {len(found)} key fields found, {len(f.get('line_items', []))} line items",
        dict(extractor=meta["used"], fields=_public_fields(f), line_items=f.get("line_items", []),
             confidence=f.get("confidence", {}), grounding=f.get("grounding", {}), evidence=f.get("evidence", {}), notes=meta["notes"],
             vendor_check=dict(status=vres["status"], name=(vres["vendor"] or {}).get("name")), po_check=po_check),
        status="warn" if input_fs or any("fell back" in n for n in meta["notes"]) else "ok")


def run(run_id, path, filename, emit, mode=None, inputs=None, review=False):
    """Process a document from file to decision.

    review=True stops once the fields are extracted and saves them, so a person can check
    them and add details before resume() runs the rest. Reading is the slow part, so it
    isn't repeated. A document that can't be read, or isn't an invoice, goes straight on.
    """
    t0 = time.time()
    path = Path(path)
    findings = []
    logger.info("processing {!r}", filename)
    start, end, skip = _stages(emit, findings)

    try:
        # 1 ---------------------------------------------------------- ingest
        start("ingest")
        ext = path.suffix.lower()
        pages = 1
        if ext == ".pdf":
            pages = len(pymupdf.open(path))
        fhash = extract.sha256_file(path)
        db.update_run(run_id, file_hash=fhash, status="running")
        end("ingest", [], f"{filename} - {path.stat().st_size / 1024:.0f} KB, {pages} page{'s' if pages != 1 else ''}",
            dict(filename=filename, kb=round(path.stat().st_size / 1024, 1), pages=pages, type=ext.lstrip(".").upper(), sha256=fhash[:16]))

        # 2 ------------------------------------------------------------ read
        start("read")
        doc = extract.read_document(path)
        how = {"text_layer": "machine-readable text layer", "ocr": "scanned image - read with OCR", "unreadable": "no readable text"}[doc["method"]]
        read_fs = []
        if doc["method"] == "unreadable" and not (doc["page_images"] and extract.llm_available() and config.EXTRACTOR != "rules"):
            if extract.ocr_available() and not (inputs or {}).get("is_invoice"):  # OCR looked and found nothing on the page
                read_fs.append(rules.F("NOT_AN_INVOICE", "reject", "read", "This file is not an invoice: no text was found in it. If it is one, upload a clearer copy."))
            else:
                read_fs.append(rules.F("UNREADABLE", "hold", "read", "No text could be read from this document and no vision model is available."))
        end("read", read_fs, f"{how} ({len(doc['text'].strip())} characters)",
            dict(method=doc["method"], chars=len(doc["text"].strip()), pages=doc["pages"], ocr_available=extract.ocr_available(),
                 preview=doc["text"].strip()[:700], notes=doc["notes"]))

        # 3 ---------------------------------------------------------- extract
        start("extract")
        f, meta = extract.extract_fields(doc, mode)
        _end_extract(end, f, meta, _apply_inputs(f, inputs or {}), run_id)
        findings[:] = [x for x in findings if x["code"] != "EXTRACT_NOTE"]  # informational only; shown in the stage

        if any(x["code"] in ("UNREADABLE", "NOT_AN_INVOICE") for x in findings):
            for sid in ("validate", "vendor", "po", "duplicates", "budget"):
                skip(sid, "document could not be read")
            return _finish(run_id, t0, emit, findings, f, meta, doc, {}, {}, None, None, None, None, [], filename)
        if review:
            db.update_run(run_id, status="review", stage_json=json.dumps(dict(fields=f, meta=meta, method=doc["method"], fhash=fhash, findings=findings)))
            logger.info("fields extracted - waiting for a person to review them")
            return None
        return _check(run_id, t0, emit, findings, f, meta, doc["method"], fhash, filename)
    except Exception as e:  # never crash a run: park it for a human with the reason
        return _failed(run_id, t0, emit, findings, filename, e)


def resume(run_id, filename, emit, inputs):
    """Run the checks on fields a person has reviewed, from where run(review=True) stopped."""
    t0 = time.time()
    state = json.loads(db.run_stage(run_id))
    findings = state["findings"]
    start, end, skip = _stages(emit, findings)
    try:
        db.update_run(run_id, status="running")
        f, meta = state["fields"], state["meta"]
        start("extract")
        _end_extract(end, f, meta, _apply_inputs(f, inputs or {}), run_id)
        findings[:] = [x for x in findings if x["code"] != "EXTRACT_NOTE"]
        return _check(run_id, t0, emit, findings, f, meta, state["method"], state["fhash"], filename)
    except Exception as e:
        return _failed(run_id, t0, emit, findings, filename, e)


def _check(run_id, t0, emit, findings, f, meta, method, fhash, filename):
    """Stages 4 to 9: validate, vendor, PO, duplicates, budget, then the decision."""
    start, end, skip = _stages(emit, findings)
    doc = dict(method=method)
    user_id = db.run_owner(run_id)  # vendors, duplicates and PO billing are all judged within this account
    billed = lambda po_number: db.billed(po_number, user_id)
    try:
        # 4 --------------------------------------------------------- validate
        start("validate")
        vendors, pos = rules.load_vendors(user_id), rules.load_pos()
        guess = next((p for p in pos if f.get("po_reference") and p["po_number"] == f["po_reference"]), None)
        d = rules.derive_amounts(f, assumed_rate=guess["gst_rate"] if guess else None)
        checks, vfs = rules.validate(f, d)
        end("validate", vfs,
            ("; ".join(c["name"] + (" ✓" if c["ok"] else " ✗") for c in checks[:4])) or "no checks possible",
            dict(doc_type=f.get("doc_type"), checks=checks,
                 derived=dict(net=d["net"], tax=d["tax"], gross=d["gross"], rate=d["rate"], basis=d["basis"], lines_sum=d["lines_sum"])))
        if any(x["code"] == "NOT_AN_INVOICE" for x in vfs):
            for sid in ("vendor", "po", "duplicates", "budget"):
                skip(sid, "not a payable invoice - nothing further to check")
            return _finish(run_id, t0, emit, findings, f, meta, doc, d, {}, None, None, None, checks, [], filename)

        # 5 ------------------------------------------------------- vendor
        start("vendor")
        vres, vfs = rules.match_vendor(f, vendors)
        batch = f.get("_batch") or {}
        if batch.get("all_vendors") and any(x["code"] == "VENDOR_UNKNOWN" for x in vfs):
            # "approve all the new vendors" was ticked for this folder. Only a new identity qualifies: a lookalike of a
            # vendor on file, or of a blocked one, is still held (vendor_approval_problem), as is any GSTIN disagreement
            name, gstin = (f.get("vendor_name") or "").strip()[:200], (f.get("vendor_gstin") or "").strip().upper()
            if name and not rules.vendor_approval_problem(name, gstin, vendors):
                db.add_vendor(rules.next_vendor_id(vendors), name, "", gstin, "approved with a folder of invoices (approve all new vendors)", run_id, user_id)
                vendors = rules.load_vendors(user_id)
                vres, vfs = rules.match_vendor(f, vendors)
                vfs.append(rules.F("VENDOR_ADDED", "info", "vendor", f"{name} was new, and was added to your approved vendors as asked for this folder."))
        v = vres["vendor"]
        vsum = (f"{v['name']} ({v['vendor_id']}) - matched by {vres['method']}, {vres['status']}" if v else
                f"'{f.get('vendor_name') or f.get('vendor_gstin') or 'unknown'}' - {vres['status'].replace('_', ' ')}")
        end("vendor", vfs, vsum,
            dict(status=vres["status"], vendor=(dict(vendor_id=v["vendor_id"], name=v["name"], gstin=v["gstin"], status=v["status"]) if v else None),
                 method=vres["method"], name_score=vres["name_score"], gstin_on_invoice=vres["gstin"], gstin_valid=vres["gstin_valid"],
                 candidates=vres["candidates"]))

        # 6 ---------------------------------------------------------- po
        start("po")
        pres, pfs = rules.match_po(f, d, v, pos, billed)
        po = pres["po"]
        ref = extract.norm_alnum(f.get("po_reference"))
        # the folder's PO answer covers the vendor approved on its first invoice, and with "approve all" every vendor it approved
        covered = bool(v and batch) and (v["vendor_id"] == batch.get("vendor")
                                         or (batch.get("all_vendors") and db.vendor_from_batch(v["vendor_id"], batch.get("id"), user_id)))
        if covered and batch.get("po") == "no_po" and any(x["code"] in rules.NO_PO_WAIVES for x in pfs):
            db.set_no_po(v["vendor_id"], "given for a folder of invoices", run_id, user_id)
        if covered and batch.get("po") == "accept" and ref and any(x["code"] == "PO_NOT_FOUND" for x in pfs):
            db.accept_po_ref(v["vendor_id"], ref, run_id, user_id)  # approved for the folder, and remembered for the vendor's later bills
        since = db.no_po_vendors(user_id).get(v["vendor_id"]) if v else None
        if since and any(x["code"] in rules.NO_PO_WAIVES for x in pfs):  # a person said this vendor bills without POs
            pfs = [x for x in pfs if x["code"] not in rules.NO_PO_WAIVES] + [rules.F(
                "PO_NOT_NEEDED", "info", "po", f"{v['name']} doesn't use purchase orders (confirmed by a person on {since[:10]}), so none is needed.")]
            pres["status"] = "not_needed"
        accepted = db.accepted_po_refs(user_id).get((v["vendor_id"], ref)) if v and ref else None
        if accepted and any(x["code"] == "PO_NOT_FOUND" for x in pfs):  # a person approved a bill citing this number before
            pfs = [x for x in pfs if x["code"] != "PO_NOT_FOUND"] + [rules.F(
                "PO_REF_ACCEPTED", "info", "po", f"{f['po_reference']} isn't in the PO register, but a person accepted it for {v['name']} on {accepted[:10]}.")]
            pres["status"] = "ref_accepted"
        held = {"PO_NOT_FOUND", "PO_NONE_OPEN", "PO_AMBIGUOUS"}  # never PO_VENDOR_MISMATCH: another vendor's PO is a fraud signal
        if batch.get("po") == "all" and any(x["code"] in held for x in pfs):  # the Approvals step: PO references approved for this upload
            pfs = [x for x in pfs if x["code"] not in held] + [rules.F("PO_APPROVED", "info", "po", (
                f"{f['po_reference']} isn't in the PO register" if f.get("po_reference") else "There is no PO on this invoice")
                + ", and PO references were approved for this upload on the Approvals step.")]
            pres["status"] = "approved_for_upload"
        end("po", pfs,
            (f"{po['po_number']} - {pres['status']} match ({po['description']})" if po else f"no PO matched ({pres['status'].replace('_', ' ')})"),
            dict(status=pres["status"], reference=pres["reference"],
                 po=(dict(po_number=po["po_number"], vendor_id=po["vendor_id"], description=po["description"], net_amount=po["net_amount"],
                          gst_rate=po["gst_rate"], po_date=po["po_date"]) if po else None),
                 candidates=pres["candidates"]))

        # 7 ------------------------------------------------------ duplicates
        start("duplicates")
        priors = db.prior_submissions(run_id, user_id)
        dres, dfs = rules.duplicate_check(f, v, fhash, priors, db.prior_by_hash(fhash, run_id, user_id))
        end("duplicates", dfs,
            (f"{len(dres['matches'])} earlier match(es): " + dres["matches"][0]["reason"]) if dres["matches"]
            else f"no duplicate among {dres['checked_against']} earlier submission(s)", dres)

        # 8 --------------------------------------------------------- budget
        start("budget")
        bres = None
        if any(x["code"] == "DUPLICATE_EXACT" for x in findings):
            skip("budget", "exact duplicate - its value is already counted on the PO, so it is not added again")
        elif po and d["net"] is not None and pres["status"] != "vendor_mismatch":
            bres, bfs = rules.budget_check(po, d["net"], billed(po["po_number"]), d["rate"])
            end("budget", bfs, f"cumulative {inr(bres['cumulative'])} vs PO {inr(bres['po_net'])} ({bres['pct_billed']:g}% billed)", bres)
        else:
            skip("budget", "no PO matched - nothing to reconcile against")

        return _finish(run_id, t0, emit, findings, f, meta, doc, d, vres, pres, dres, bres, checks, [], filename)
    except Exception as e:
        return _failed(run_id, t0, emit, findings, filename, e)


def _failed(run_id, t0, emit, findings, filename, e):
    """Park the run for a person with the reason, rather than crash."""
    logger.exception("processing failed for {!r} - held for manual handling", filename)
    fs = [rules.F("PROCESSING_ERROR", "hold", "decision", f"Processing failed unexpectedly ({type(e).__name__}: {str(e)[:160]}). Needs manual handling.")]
    findings.extend(fs)
    emit("decision", "fail", "Decision", "processing error", {})
    res = dict(decision="HOLD", headline="Held for review - the system hit an error on this document.", findings=findings,
               next_steps=["Process this invoice manually; the error has been logged."], fields={}, message=None)
    db.update_run(run_id, status="error", decision="HOLD", result_json=json.dumps(res), duration_ms=int((time.time() - t0) * 1000))
    return res


def file_in(run_id, folder_id):
    """Put a decided invoice in the folder chosen for its upload, unless a person already filed it. Returns {id, name} or None."""
    user_id = db.run_owner(run_id)
    row, folder = db.get_run(run_id, user_id), db.get_folder(folder_id, user_id)
    if not row or not folder or row["folder_id"] not in (None, folder["id"]):
        return None
    db.update_run(run_id, folder_id=folder["id"])
    return folder


def file_with_vendor(run_id, vendor_id):
    """An approved invoice not yet in a folder goes where this vendor's earlier bills went. Returns {id, name} or None."""
    user_id = db.run_owner(run_id)
    row = db.get_run(run_id, user_id)
    if not vendor_id or not row or row["folder_id"] is not None:
        return None
    folder = db.get_folder(db.vendor_folder(vendor_id, run_id, user_id), user_id)
    if folder:
        db.update_run(run_id, folder_id=folder["id"])
        logger.info("filed in {!r} with earlier bills from vendor {}", folder["name"], vendor_id)
    return folder


def _finish(run_id, t0, emit, findings, f, meta, doc, d, vres, pres, dres, bres, checks, _unused, filename):
    sid = "decision"
    emit(sid, "running", TITLES[sid], "", {})
    if config.STAGE_DELAY:
        time.sleep(config.STAGE_DELAY)
    decision = rules.decide(findings)
    ex = rules.explain(decision, findings, f, d, pres, bres)
    vendor = (vres or {}).get("vendor")
    msg = rules.draft_message(decision, f, vendor, findings)
    po = (pres or {}).get("po")
    result = dict(
        decision=decision, headline=ex["headline"], next_steps=ex["next_steps"], message=msg,
        findings=sorted(findings, key=lambda x: -rules.SEV[x["severity"]]),
        fields=_public_fields(f), line_items=f.get("line_items", []),
        derived=dict(net=d.get("net"), tax=d.get("tax"), gross=d.get("gross"), rate=d.get("rate"), basis=d.get("basis")) if d else {},
        vendor=dict(vendor_id=vendor["vendor_id"], name=vendor["name"], status=vres["status"]) if vendor else None,
        po=dict(po_number=po["po_number"], description=po["description"], match=pres["status"]) if po else None,
        budget=bres, checks=checks, extractor=meta["used"], read_method=doc["method"])
    ms = int((time.time() - t0) * 1000)
    # record on the PO ledger only when approved: the NEXT invoice is judged on cumulative billing
    if decision == "APPROVE" and po and bres:
        db.add_ledger(po["po_number"], run_id, bres["this_net"])
    chosen = (f.get("_batch") or {}).get("folder")  # picked on the Approvals step for the whole upload
    if chosen and decision != "REJECT":
        folder = file_in(run_id, chosen)
    else:
        folder = file_with_vendor(run_id, vendor["vendor_id"] if vendor else None) if decision == "APPROVE" else None
    if folder:  # why: the folder chosen on the Approvals step, or the one holding this vendor's earlier bills
        result["auto_folder"] = dict(folder, why="approvals" if chosen and decision != "REJECT" else "vendor")
    db.update_run(run_id, status="done", decision=decision, vendor_name=f.get("vendor_name"), vendor_id=vendor["vendor_id"] if vendor else None,
                  invoice_number=f.get("invoice_number"), invoice_date=f.get("invoice_date"), po_number=po["po_number"] if po else None,
                  net=d.get("net") if d else None, total=f.get("total"), extractor=meta["used"], duration_ms=ms,
                  result_json=json.dumps(result), **({"folder_id": None} if decision == "REJECT" else {}))  # a re-check can reject a filed invoice
    logger.info("{} in {} ms {}", decision, ms, sorted({x["code"] for x in findings if x["severity"] in ("hold", "reject")}))
    sym ={"APPROVE": "ok", "HOLD": "warn", "REJECT": "fail"}[decision]
    emit(sid, sym, TITLES[sid], {"APPROVE": "Approved for payment", "HOLD": "Held for review", "REJECT": "Rejected"}[decision], dict(decision=decision, headline=ex["headline"], next_steps=ex["next_steps"], duration_ms=ms))
    return result
