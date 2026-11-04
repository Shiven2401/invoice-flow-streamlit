"""FastAPI app: upload -> background pipeline -> live SSE stream -> history / ledger / stats.

Runs are processed one at a time by a single worker. That is deliberate: the PO
ledger must be updated in order, otherwise two split invoices processed in
parallel could both see the same 'billed so far' and both pass.

Every /api route needs a login (see auth.py) and sees only that account's data.
"""
import json
import queue
import re
import shutil
import threading
import time
import uuid
from collections import Counter
from datetime import datetime
import zipfile
from pathlib import Path, PurePosixPath

import pymupdf
from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger

from . import auth, config, db, export, extract, logs, pipeline, rules

logs.setup()
logger.info("extractor={} llm={} ocr={} logs={}", config.EXTRACTOR, "{} ({})".format(*extract.llm_provider()) if extract.llm_available() else False,
            extract.ocr_available(), config.LOG_DIR)
if extract.llm_key_problem():
    logger.warning("{} - using the built-in rules and OCR", extract.llm_key_problem())
interrupted = db.fail_interrupted()
if interrupted:
    logger.warning("{} run(s) were interrupted by a restart and need a person", interrupted)
for _run_id, _path in db.purge_hidden():
    Path(_path).unlink(missing_ok=True)
    logger.bind(run=_run_id).info("removed earlier, now deleted for good")

app = FastAPI(title="Invoice Flow", docs_url=None, redoc_url=None, openapi_url=None)
app.include_router(auth.router)
BUS = {}                     # run_id -> {events, done, t0}
JOBS = queue.Queue()
ALLOWED = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
KEEP_FINISHED = 200          # finished runs kept in memory; older ones replay from the database
CANCELLED = set()            # run ids a person has cancelled; the worker drops them at the next stage


class Cancelled(BaseException):
    """Raised at a stage boundary once a run is cancelled. Not an Exception, so the pipeline's
    "park it for a person" handler doesn't catch it: a cancelled document is discarded, not held."""
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
SECURITY_HEADERS = [
    (b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"), (b"referrer-policy", b"same-origin"),
    (b"content-security-policy", b"default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                 b"object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"),
]


class Guard:
    """Security headers on every response, and the upload limit, checked before any of the body is read.
    Plain ASGI rather than BaseHTTPMiddleware, which doesn't sit well in front of the live event stream."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.inner(scope, receive, send)
        if scope["method"] == "POST" and scope["path"] == "/api/runs":
            length = dict(scope["headers"]).get(b"content-length")
            if not length or int(length) > config.MAX_UPLOAD_MB * 1024 * 1024:
                return await JSONResponse(dict(detail=f"Uploads are limited to {config.MAX_UPLOAD_MB} MB."), 413)(scope, receive, send)
        private = scope["path"].startswith("/api/")

        async def with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", [])) + SECURITY_HEADERS
                if private and not any(k.lower() == b"cache-control" for k, _v in headers):
                    headers.append((b"cache-control", b"no-store"))  # invoices stay out of shared browser caches
                message["headers"] = headers
            await send(message)
        await self.inner(scope, receive, with_headers)


app.add_middleware(Guard)


def _worker():
    while True:
        run_id, path, filename, inputs, kind = JOBS.get()
        if run_id not in BUS or not db.run_owner(run_id):    # cancelled while it waited in the queue
            CANCELLED.discard(run_id)
            JOBS.task_done()
            continue
        bus = BUS[run_id]
        bus["t0"] = time.time()

        def emit(stage, status, title, summary, detail):
            if run_id in CANCELLED:
                raise Cancelled()
            bus["events"].append(dict(seq=len(bus["events"]), t=round(time.time() - bus["t0"], 2), stage=stage,
                                      status=status, title=title, summary=summary, detail=detail))
        with logger.contextualize(run=run_id):
            try:
                if kind == "resume":
                    pipeline.resume(run_id, filename, emit, inputs)
                else:
                    pipeline.run(run_id, path, filename, emit, inputs=inputs, review=kind == "review")
            except Cancelled:
                owner = db.run_owner(run_id)
                batch_id = (db.get_run(run_id, owner) or {}).get("batch_id") if owner else None
                stored = db.delete_run(run_id, owner) if owner else None
                if stored:
                    Path(stored).unlink(missing_ok=True)
                logger.info("cancelled by a person - discarded")
                if kind == "review":
                    _promote_batch(batch_id, owner)
            except Exception:
                logger.exception("pipeline crashed")
            finally:
                CANCELLED.discard(run_id)
                try:
                    db.update_run(run_id, events_json=json.dumps(bus["events"]))
                except Exception:
                    logger.exception("could not save run events")
                if kind == "review":  # a folder's first invoice that won't stop at Review hands the question to the next
                    try:
                        owner = db.run_owner(run_id)
                        row = db.get_run(run_id, owner) if owner else None
                        if row and row["status"] != "review":  # a cancelled one is gone: the cancel handler passed it on
                            _promote_batch(row["batch_id"], owner)
                    except Exception:
                        logger.exception("could not pass the folder's review on")
                bus["done"] = True
                for old in [k for k, b in list(BUS.items()) if b["done"]][:-KEEP_FINISHED]:
                    BUS.pop(old, None)
                JOBS.task_done()


threading.Thread(target=_worker, daemon=True).start()


def _resume_stuck_batches():
    for batch_id, user_id in db.stuck_batches():
        logger.info("folder {}: its first invoice was interrupted, so the next one asks for the approval", batch_id)
        _promote_batch(batch_id, user_id)


def _check_capacity(user_id, adding):
    """Optional limits (0 = none, the default): see config.MAX_QUEUED_PER_USER and MAX_RUNS_PER_USER."""
    if config.MAX_QUEUED_PER_USER and db.open_runs(user_id, waiting=True) + adding > config.MAX_QUEUED_PER_USER:
        raise HTTPException(429, f"You can have up to {config.MAX_QUEUED_PER_USER} documents waiting. Let these finish first.")
    if config.MAX_RUNS_PER_USER and db.count_runs(user_id) + adding > config.MAX_RUNS_PER_USER:
        raise HTTPException(429, f"This account has reached its limit of {config.MAX_RUNS_PER_USER} documents.")


def _enqueue(src: Path, filename: str, user_id, inputs=None, review=False, batch_id=None, wait=False):
    """Store an upload and queue it. wait=True stores a folder's later invoice without queuing it: it waits for the
    approval given on the folder's first invoice (see _release_batch)."""
    run_id = uuid.uuid4().hex[:8]
    name = Path(filename).name[:200]
    dest = config.UPLOAD_DIR / f"{run_id}{Path(filename).suffix.lower()}"  # never build a disk path from an uploaded name
    shutil.copyfile(src, dest)
    db.create_run(run_id, name, dest, user_id, inputs=inputs, batch_id=batch_id, status="waiting" if wait else "queued")
    if wait:
        logger.bind(run=run_id).info("{!r} waits for the approval of its folder's first invoice", name)
        return run_id
    BUS[run_id] = dict(events=[], done=False, t0=time.time())
    JOBS.put((run_id, dest, name, inputs or {}, "review" if review else "full"))
    logger.bind(run=run_id).info("queued {!r}", name)
    return run_id


def _queue_stored(row, kind, inputs=None):
    """Queue a run that is already stored: a folder's waiting invoice."""
    inputs = dict(json.loads(row["inputs_json"] or "{}"), **(inputs or {}))
    db.update_run(row["id"], status="queued", inputs_json=json.dumps(inputs))
    BUS[row["id"]] = dict(events=[], done=False, t0=time.time())
    JOBS.put((row["id"], Path(row["file_path"]), row["filename"], inputs, kind))
    logger.bind(run=row["id"]).info("queued {!r} ({})", row["filename"], kind)


def _promote_batch(batch_id, user_id):
    """The folder's first invoice was discarded, or decided without stopping at Review (unreadable, not an invoice),
    so its approval never comes: the next waiting invoice stops at Review instead and asks for it."""
    waiting = db.batch_waiting(batch_id, user_id) if batch_id else []
    if waiting:
        _queue_stored(waiting[0], "review")


def _release_batch(batch_id, user_id, inputs):
    """The folder's first invoice was reviewed: check the rest with its answers, straight through to a decision."""
    rows = db.batch_waiting(batch_id, user_id) if batch_id else []
    for row in rows:
        _queue_stored(row, "full", inputs)
    if rows:
        logger.info("folder {}: {} more invoice(s) checked with the first one's approval", batch_id, len(rows))
    return [dict(id=r["id"], filename=r["filename"]) for r in rows]


def _requeue(row):
    """Check a stored invoice again under the same run id, so it isn't caught as a duplicate of itself."""
    run_id = row["id"]
    db.update_run(run_id, status="queued", decision=None, result_json=None, events_json=None, duration_ms=None)
    BUS[run_id] = dict(events=[], done=False, t0=time.time())
    JOBS.put((run_id, Path(row["file_path"]), row["filename"], json.loads(row["inputs_json"] or "{}"), "full"))
    logger.bind(run=run_id).info("queued {!r} for a re-check", row["filename"])


@app.post("/api/runs")
async def upload(files: list[UploadFile] = File(...), inputs: str = Form("{}"), batch: str = Form(""), approvals: str = Form(""),
                 user: dict = Depends(auth.current_user)):
    """Each file stops at Review for a person. batch=1 uploads a folder: only its first invoice stops at Review.

    approvals: answers from the Approvals step, given before anything is read, for every file in the upload:
    {"vendors": approve new vendors, "po": approve PO references whether present or not, "folder_id": file them here}.
    Either way the rest of a folder waits for the first one's review (where, without approvals, the vendor and PO answers
    are given), then each invoice is checked to a decision in turn, the first one included, before the next begins."""
    try:
        typed = extract.clean_inputs(json.loads(inputs))
        given = json.loads(approvals) if isinstance(approvals, str) and approvals else None  # not a str when called directly
    except ValueError as e:
        raise HTTPException(400, str(e))
    if given is not None and not isinstance(given, dict):
        raise HTTPException(400, "approvals must be an object, such as {\"vendors\": true, \"po\": false, \"folder_id\": null}")
    if given is not None:
        folder_id = given.get("folder_id")
        if folder_id is not None and not (isinstance(folder_id, int) and db.get_folder(folder_id, user["id"])):
            raise HTTPException(404, "folder not found")
    several = "Those details describe one invoice, so upload one file at a time to use them. A note can go with several files."
    if len(files) > 1 and set(typed) - {"note"}:  # refused before anything is read; a single ZIP is checked once unpacked
        raise HTTPException(400, several)
    for uf in files:
        ext = Path(uf.filename or "").suffix.lower()
        if ext not in ALLOWED and ext != ".zip":
            logger.warning("rejected upload {!r}: unsupported file type", uf.filename)
            raise HTTPException(400, f"Unsupported file type '{ext}'. Upload PDFs, photos or a ZIP of them.")
    items = []  # (name, temp file): every invoice in the upload, with any ZIP unpacked into its contents
    try:
        for uf in files:
            if Path(uf.filename or "").suffix.lower() == ".zip":
                items += _unpack_zip(uf)
            else:
                tmp = config.UPLOAD_DIR / f"_tmp_{uuid.uuid4().hex}"
                with open(tmp, "wb") as fh:
                    shutil.copyfileobj(uf.file, fh)
                items.append((Path(uf.filename).name[:200], tmp))
        if len(items) > 1 and set(typed) - {"note"}:
            raise HTTPException(400, several)
        _check_capacity(user["id"], len(items))
        # several invoices are one batch: a folder, several files, or a ZIP holding more than one
        zipped = any(Path(uf.filename or "").suffix.lower() == ".zip" for uf in files)
        batch_id = uuid.uuid4().hex[:8] if (batch == "1" or zipped) and len(items) > 1 else None
        if given is not None:  # the checks apply these (pipeline._check, _finish)
            typed = dict(typed, batch=dict(id=batch_id, vendor=None, po="all" if given.get("po") else None,
                                           all_vendors=bool(given.get("vendors")), folder=given.get("folder_id")))
        out = []
        for i, (name, tmp) in enumerate(items):
            later = bool(batch_id) and i > 0
            # a person reviews the fields before the checks: every file, or only a batch's first. The rest of a batch
            # wait until that review is submitted, then go through one at a time behind its checks: the worker takes jobs
            # in order, so queuing them now would put the first invoice's checks after all of theirs
            rid = _enqueue(tmp, name, user["id"], typed, review=not later, batch_id=batch_id, wait=later)
            out.append(dict(id=rid, filename=name, batch=batch_id, waiting=later, review=not later))
    finally:
        for _, tmp in items:
            tmp.unlink(missing_ok=True)
    if batch_id:
        logger.info("batch {} uploaded: {} invoices, the first stops at Review", batch_id, len(items))
    return dict(runs=out)


ZIP_MAX_UNPACKED_MB = 2000   # everything in one archive, once unpacked: a size guard against zip bombs, not a count limit


def _unpack_zip(uf):
    """The PDFs and photos inside an uploaded ZIP, subfolders included, as (name, temp file) in path order.

    Member names only become display names: files are written under names chosen here, so a path inside the archive
    ("../../x") can't place anything. Sizes are counted while unpacking, not taken from the archive's own headers,
    which a crafted ZIP (a "zip bomb") can understate. Password-protected archives are refused."""
    try:
        archive = zipfile.ZipFile(uf.file)
    except zipfile.BadZipFile:
        raise HTTPException(400, f"'{uf.filename}' isn't a readable ZIP file.")
    picked = []
    for info in archive.infolist():
        parts = PurePosixPath(info.filename.replace("\\", "/")).parts
        name = parts[-1] if parts else ""
        if info.is_dir() or not name or name.startswith(".") or "__MACOSX" in parts or Path(name).suffix.lower() not in ALLOWED:
            continue  # folders, system files, nested ZIPs and anything that isn't a PDF or photo
        if info.flag_bits & 0x1:
            raise HTTPException(400, f"'{uf.filename}' is password-protected. Upload it unprotected.")
        picked.append((info, name[:200]))
    if not picked:
        raise HTTPException(400, f"'{uf.filename}' has no PDFs or photos in it.")
    natural = lambda s: [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]
    picked.sort(key=lambda p: natural(p[0].filename))
    per_file, budget, out = config.MAX_UPLOAD_MB * 1024 * 1024, ZIP_MAX_UNPACKED_MB * 1024 * 1024, []
    try:
        for info, name in picked:
            tmp = config.UPLOAD_DIR / f"_tmp_{uuid.uuid4().hex}"
            out.append((name, tmp))
            written = 0
            with archive.open(info) as src, open(tmp, "wb") as dst:
                while chunk := src.read(1 << 20):
                    written += len(chunk)
                    budget -= len(chunk)
                    if written > per_file or budget < 0:
                        raise HTTPException(400, f"'{uf.filename}' unpacks to more than the size allowed ({config.MAX_UPLOAD_MB} MB per file, "
                                                 f"{ZIP_MAX_UNPACKED_MB} MB in all).")
                    dst.write(chunk)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as e:  # damaged, or a compression method zipfile can't read
        for _, tmp in out:
            tmp.unlink(missing_ok=True)
        raise HTTPException(400, f"'{uf.filename}' couldn't be unpacked ({type(e).__name__}).")
    except HTTPException:
        for _, tmp in out:
            tmp.unlink(missing_ok=True)
        raise
    logger.info("unpacked {!r}: {} invoice(s)", uf.filename, len(out))
    return out


@app.post("/api/runs/sample")
def run_samples(body: dict = Body(...), user: dict = Depends(auth.current_user)):
    manifest = {m["file"]: m for m in json.loads((config.SAMPLES_DIR / "synthetic" / "manifest.json").read_text())}
    names = body.get("files", [])
    for name in names:
        if name not in manifest:
            raise HTTPException(404, f"Unknown sample '{name}'")
    _check_capacity(user["id"], len(names))
    return dict(runs=[dict(id=_enqueue(config.SAMPLES_DIR / "synthetic" / name, name, user["id"]), filename=name) for name in names])


@app.get("/api/runs/{run_id}/stream")
def stream(run_id: str, user: dict = Depends(auth.current_user)):
    row = db.get_run(run_id, user["id"])
    if not row:
        raise HTTPException(404, "run not found")
    bus = BUS.get(run_id)

    def gen():
        if bus is None:  # server restarted or run finished long ago: replay what was stored
            for ev in json.loads(row.get("events_json") or "[]"):
                yield f"data: {json.dumps(ev)}\n\n"
            yield "event: end\ndata: {}\n\n"
            return
        i = 0
        while True:
            while i < len(bus["events"]):
                yield f"data: {json.dumps(bus['events'][i])}\n\n"
                i += 1
            if bus["done"] and i >= len(bus["events"]):
                yield "event: end\ndata: {}\n\n"
                return
            time.sleep(0.06)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/runs")
def runs(user: dict = Depends(auth.current_user)):
    return dict(runs=db.list_runs(user["id"], limit=-1))


@app.get("/api/runs/{run_id}")
def run_detail(run_id: str, user: dict = Depends(auth.current_user)):
    row = db.get_run(run_id, user["id"])
    if not row:
        raise HTTPException(404, "run not found")
    row["result"] = json.loads(row.pop("result_json") or "null")
    row["events"] = json.loads(row.pop("events_json") or "[]")
    row["inputs"] = json.loads(row.pop("inputs_json") or "{}")
    row.pop("file_path", None)
    if row["decision"] == "HOLD" and not row["final_decision"] and row["vendor_id"]:  # for answering a PO hold
        row["po_options"] = _open_pos(row["vendor_id"], user["id"])
    return row


@app.get("/api/runs/{run_id}/file")
def run_file(run_id: str, user: dict = Depends(auth.current_user)):
    row = db.get_run(run_id, user["id"])
    if not row or not Path(row["file_path"]).exists():
        raise HTTPException(404, "file not found")
    return FileResponse(row["file_path"], filename=row["filename"], content_disposition_type="inline")


@app.get("/api/runs/{run_id}/preview")
def run_preview(run_id: str, user: dict = Depends(auth.current_user), large: bool = False):
    """Page 1 as an image: identical in every browser, no PDF plugin needed. large=1 renders a PDF sharper, for the
    enlarged view (a photo is always sent as it was uploaded)."""
    row = db.get_run(run_id, user["id"])
    if not row or not Path(row["file_path"]).exists():
        raise HTTPException(404, "file not found")
    p = Path(row["file_path"])
    cache = {"Cache-Control": "private, max-age=3600"}  # the step pages redraw often; a run's file never changes
    if p.suffix.lower() == ".pdf":
        doc = pymupdf.open(p)
        return Response(doc[0].get_pixmap(dpi=200 if large else 120).tobytes("png"), media_type="image/png", headers=cache)
    return FileResponse(p, headers=cache)


@app.post("/api/runs/{run_id}/continue")
def continue_run(run_id: str, body: dict = Body(...), user: dict = Depends(auth.current_user)):
    """A person has reviewed the extracted fields, perhaps adding details or approving a new vendor. Run the remaining checks.

    approve_vendor: {"note": ...} adds the vendor named on the Review page to this account's approved list first, under
    the same rules as approving it later: never a blocked vendor, one already on file, or a name resembling either.
    """
    row = db.get_run(run_id, user["id"])
    if not row:
        raise HTTPException(404, "run not found")
    if row["status"] != "review":
        raise HTTPException(409, "this document isn't waiting for review")
    try:
        typed = extract.clean_inputs(body.get("inputs") or {})
    except ValueError as e:
        raise HTTPException(400, str(e))
    approval, batch_po, all_vendors = body.get("approve_vendor"), body.get("batch_po"), bool(body.get("approve_all_vendors"))
    if approval is not None and not isinstance(approval, dict):
        raise HTTPException(400, "approve_vendor must be an object, such as {\"note\": \"...\"}")
    if batch_po not in (None, "accept", "no_po"):
        raise HTTPException(400, "batch_po must be \"accept\" or \"no_po\"")
    if (batch_po or all_vendors) and not row["batch_id"]:
        raise HTTPException(400, "Answers for a whole folder can only be given on a folder's first invoice.")
    read = json.loads(db.run_stage(run_id))["fields"]
    read_name = read.get("vendor_name") or ""
    name = (typed.get("vendor_name") or read_name).strip()[:200]
    gstin = (typed.get("vendor_gstin") or read.get("vendor_gstin") or "").strip().upper()
    approved = lambda: rules.match_vendor(dict(vendor_name=name, vendor_gstin=gstin), rules.load_vendors(user["id"]))[0]
    if batch_po and not all_vendors and approval is None and approved()["status"] != "approved" \
            and not (json.loads(row["inputs_json"] or "{}").get("batch") or {}).get("all_vendors"):
        raise HTTPException(400, "Approve the vendor too, so the PO answer knows whose purchase orders these are.")
    if approval is not None:
        vendors = rules.load_vendors(user["id"])
        problem = rules.vendor_approval_problem(name, gstin, vendors)
        if problem:
            raise HTTPException(400, problem)
        # a corrected name keeps what the document reads as an alias, so this invoice still matches in the checks
        vendor_id, note = rules.next_vendor_id(vendors), str(approval.get("note") or "").strip()[:500]
        db.add_vendor(vendor_id, name, read_name if read_name != name else "", gstin, note, run_id, user["id"])
        if approval.get("no_po"):
            db.set_no_po(vendor_id, note, run_id, user["id"])
    # given here once for the folder: the PO answer for this vendor (and, with "approve all", for every vendor the folder
    # approves), and whether new vendors are approved as their invoices are checked. The checks apply them (pipeline._check)
    shared, before = {}, json.loads(row["inputs_json"] or "{}").get("batch") or {}  # before: from the Approvals step, if it was used
    if row["batch_id"]:
        vendor = approved()["vendor"] if approved()["status"] == "approved" else None
        if batch_po and not vendor and not all_vendors and not before.get("all_vendors"):
            raise HTTPException(400, "The vendor isn't cleanly on your approved list, so the PO answer can't be shared.")
        shared = dict(batch=dict(before, id=row["batch_id"], vendor=vendor["vendor_id"] if vendor else before.get("vendor"),
                                 po=batch_po or before.get("po"), all_vendors=all_vendors or bool(before.get("all_vendors"))))
    inputs = dict(json.loads(row["inputs_json"] or "{}"), **typed, **shared)
    db.update_run(run_id, status="queued", inputs_json=json.dumps(inputs))
    BUS[run_id] = dict(events=json.loads(row["events_json"] or "[]"), done=False, t0=time.time())  # the trail carries on
    JOBS.put((run_id, Path(row["file_path"]), row["filename"], inputs, "resume"))
    logger.bind(run=run_id).info("reviewed - running the checks ({} detail(s) added)", len(typed))
    rest = _release_batch(row["batch_id"], user["id"], shared)
    return dict(ok=True, run=dict(id=run_id, filename=row["filename"]), batch=rest)


@app.post("/api/runs/{run_id}/override")
def override(run_id: str, body: dict = Body(...), user: dict = Depends(auth.current_user)):
    """A person resolves a HOLD. Approving it posts it to the PO ledger, same as an auto-approval."""
    row = db.get_run(run_id, user["id"])
    if not row or row["status"] != "done":
        raise HTTPException(404, "run not found or not finished")
    if row["decision"] != "HOLD":
        raise HTTPException(400, "only held invoices can be resolved manually")
    decision = (body.get("decision") or "").upper()
    if decision not in ("APPROVE", "REJECT"):
        raise HTTPException(400, "decision must be APPROVE or REJECT")
    res = json.loads(row["result_json"])
    db.update_run(run_id, final_decision=decision, override_note=(body.get("note") or "").strip()[:500],
                  **({"folder_id": None} if decision == "REJECT" else {}))  # rejected invoices don't stay in folders
    logger.bind(run=run_id).info("held invoice manually resolved: {}", decision)
    if decision == "APPROVE" and row["po_number"] and res.get("budget"):
        db.add_ledger(row["po_number"], run_id, res["budget"]["this_net"])
    else:
        db.remove_ledger(run_id)
    learned, folder = [], None
    if decision == "APPROVE":
        vendor_id, learned = _learn_from_approval(row, res, user["id"])
        if learned:
            db.update_run(run_id, vendor_id=vendor_id, result_json=json.dumps(dict(res, learned=learned)))
        folder = pipeline.file_with_vendor(run_id, vendor_id)
    return dict(ok=True, final_decision=decision, folder=folder, learned=learned)


def _learn_from_approval(row, res, user_id):
    """A person approved a bill the checks held over its vendor or PO. Remember their answer, so the vendor's next
    bills pass those checks by themselves (every other check still runs). Returns (vendor id, what was learned).

    Vendor: an unknown name joins the approved list. An ambiguous name with no GSTIN becomes another name for the
    approved vendor it resembles, keeping that vendor's POs; with its own GSTIN it's a separate vendor.
    PO: a cited number missing from the register is accepted for the vendor; a bill citing none marks the vendor as
    not using POs. Fraud signals are never learned: a GSTIN that disagrees, a lookalike of a blocked vendor, another
    vendor's PO. Those stay on the list of things a person has to look at every time.
    """
    codes = {x["code"] for x in res.get("findings", [])}
    f = res.get("fields") or {}
    name, gstin = (f.get("vendor_name") or "").strip()[:200], (f.get("vendor_gstin") or "").strip().upper()
    vendor_id, learned, vendors, new_vendor = row["vendor_id"], [], rules.load_vendors(user_id), False
    if codes & {"VENDOR_UNKNOWN", "VENDOR_AMBIGUOUS"} and name and not (gstin and not rules.GSTIN_RE.match(gstin)):
        look = [(s, v) for s, v in rules.best_vendor_scores(name, vendors) if s >= config.RULES["vendor_name_weak"]]
        if any(v["status"].lower() != "approved" for _, v in look):
            pass  # resembles a blocked vendor
        elif look and not gstin:
            closest = look[0][1]
            db.add_alias(closest["vendor_id"], name, row["id"], user_id)
            vendor_id = closest["vendor_id"]
            learned.append(f"'{name}' is now known as another name for {closest['name']}, so its bills match that vendor.")
        elif not (gstin and any(v["gstin"].upper() == gstin for v in vendors)):
            vendor_id, new_vendor = rules.next_vendor_id(vendors), True
            db.add_vendor(vendor_id, name, "", gstin, "added when a person approved a held invoice", row["id"], user_id)
            learned.append(f"{name} was added to your approved vendors.")
    if vendor_id:
        ref = f.get("po_reference")
        if "PO_NOT_FOUND" in codes and ref:
            db.accept_po_ref(vendor_id, extract.norm_alnum(ref), row["id"], user_id)
            learned.append(f"PO number {ref} is accepted for this vendor from now on.")
        elif codes & rules.NO_PO_WAIVES or (new_vendor and not ref and "PO_NOT_CHECKED" in codes):
            db.set_no_po(vendor_id, "learned when a person approved a held invoice", row["id"], user_id)
            learned.append("This vendor is marked as not using purchase orders, so its bills aren't held for one.")
    if learned:
        logger.bind(run=row["id"]).info("learned from a manual approval: {}", " ".join(learned))
    return vendor_id, learned


@app.post("/api/runs/{run_id}/approve-vendor")
def approve_vendor(run_id: str, body: dict = Body(...), user: dict = Depends(auth.current_user)):
    """A person vouches for a vendor the system doesn't know. It joins their approved list and the invoice is re-checked.
    Only VENDOR_UNKNOWN qualifies: blocked vendors and identity mismatches can't be approved away here."""
    row = db.get_run(run_id, user["id"])
    if not row or row["status"] != "done":
        raise HTTPException(404, "run not found or not finished")
    res = json.loads(row["result_json"] or "{}")
    if row["final_decision"] or row["decision"] != "HOLD" or not any(x["code"] == "VENDOR_UNKNOWN" for x in res.get("findings", [])):
        raise HTTPException(400, "only an open invoice held for an unknown vendor can have its vendor approved here")
    fields = res.get("fields") or {}
    read_name = fields.get("vendor_name") or ""
    name = str(body.get("name") or read_name).strip()[:200]
    gstin = str(body.get("gstin", fields.get("vendor_gstin")) or "").strip().upper()
    vendors = rules.load_vendors(user["id"])
    problem = rules.vendor_approval_problem(name, gstin, vendors)
    if problem:
        raise HTTPException(400, problem)
    vendor_id, note = rules.next_vendor_id(vendors), str(body.get("note") or "").strip()[:500]
    # if the name was corrected, keep what the document reads as an alias so this invoice still matches on the re-check
    aliases = read_name if read_name != name else ""
    db.add_vendor(vendor_id, name, aliases, gstin, note, run_id, user["id"])
    if body.get("no_po"):  # a new vendor has no POs on file, so without this its bills would all be held for one
        db.set_no_po(vendor_id, note, run_id, user["id"])
    _requeue(row)
    return dict(ok=True, vendor_id=vendor_id, run=dict(id=run_id, filename=row["filename"]))


def _open_pos(vendor_id, user_id):
    """The vendor's open POs that still have a balance: what a person can match a held invoice to."""
    out = []
    for p in rules.load_pos():
        if p["vendor_id"] == vendor_id and p["status"] == "open":
            remaining = round(p["net_amount"] - db.billed(p["po_number"], user_id), 2)
            if remaining > 0.005:
                out.append(dict(po_number=p["po_number"], description=p["description"], remaining=remaining))
    return out


@app.post("/api/runs/{run_id}/po")
def answer_po(run_id: str, body: dict = Body(...), user: dict = Depends(auth.current_user)):
    """A person answers a PO hold: {"po_number": ...} matches the invoice to one of the vendor's open POs, or
    {"no_po": true} records that the vendor bills without POs, which also applies to its later bills. Re-checked either way.
    Going without a PO is only for a bill that cites none: one that cites a PO nobody can find needs the right PO."""
    row = db.get_run(run_id, user["id"])
    if not row or row["status"] != "done":
        raise HTTPException(404, "run not found or not finished")
    codes = {x["code"] for x in json.loads(row["result_json"] or "{}").get("findings", [])}
    if row["final_decision"] or row["decision"] != "HOLD" or not codes & rules.PO_HOLDS:
        raise HTTPException(400, "only an open invoice held over its purchase order can be answered here")
    if not row["vendor_id"]:
        raise HTTPException(400, "The vendor has to be identified first.")
    note = str(body.get("note") or "").strip()[:500]
    if body.get("no_po"):
        if not codes & rules.NO_PO_WAIVES:
            raise HTTPException(400, "This invoice cites a PO number, so it needs the right PO rather than none.")
        db.set_no_po(row["vendor_id"], note, run_id, user["id"])
    else:
        number = str(body.get("po_number") or "")
        if number not in {p["po_number"] for p in _open_pos(row["vendor_id"], user["id"])}:
            raise HTTPException(400, f"'{number}' isn't an open PO with a balance for this vendor.")
        db.update_run(run_id, inputs_json=json.dumps(dict(json.loads(row["inputs_json"] or "{}"), po_assigned=number)))
    logger.bind(run=run_id).info("PO hold answered: {}", "vendor needs no PO" if body.get("no_po") else body.get("po_number"))
    _requeue(db.get_run(run_id, user["id"]))
    return dict(ok=True, run=dict(id=run_id, filename=row["filename"]))


@app.post("/api/runs/{run_id}/treat-as-invoice")
def treat_as_invoice(run_id: str, user: dict = Depends(auth.current_user)):
    """A person overrules "not an invoice". The automatic check is a judgment call, so it never gets the last word.
    The document is checked again but ends up held, never auto-approved, because nothing has confirmed it."""
    row = db.get_run(run_id, user["id"])
    if not row or row["status"] != "done":
        raise HTTPException(404, "run not found or not finished")
    if not any(x["code"] == "NOT_AN_INVOICE" for x in json.loads(row["result_json"] or "{}").get("findings", [])):
        raise HTTPException(400, "only a document rejected as not an invoice can be checked this way")
    db.update_run(run_id, inputs_json=json.dumps(dict(json.loads(row["inputs_json"] or "{}"), is_invoice=True)))
    logger.bind(run=run_id).info("a person marked {!r} as an invoice", row["filename"])
    _requeue(db.get_run(run_id, user["id"]))
    return dict(ok=True, run=dict(id=run_id, filename=row["filename"]))


@app.delete("/api/vendors/{vendor_id}")
def remove_vendor(vendor_id: str, user: dict = Depends(auth.current_user)):
    """Undo an approval. Only vendors approved in the app can go: the master list isn't edited from here."""
    if not db.remove_vendor(vendor_id, user["id"]):
        raise HTTPException(404, "not a vendor approved in the app")
    return dict(ok=True)


@app.post("/api/reset")
def reset(user: dict = Depends(auth.current_user)):
    """Clear this account only. Waits for its own runs: the shared queue may be busy with other people's."""
    deadline = time.time() + 300
    while db.open_runs(user["id"]):
        if time.time() > deadline:
            raise HTTPException(409, "Documents are still being processed. Try again shortly.")
        time.sleep(0.2)
    for run_id, path in db.reset(user["id"]):
        BUS.pop(run_id, None)
        Path(path).unlink(missing_ok=True)
    logger.info("reset for {!r}: run history, PO ledger, approved vendors and uploads cleared", user["username"])
    return dict(ok=True)


@app.get("/api/folders")
def folders(user: dict = Depends(auth.current_user)):
    return dict(folders=db.list_folders(user["id"]))


@app.post("/api/folders")
def create_folder(body: dict = Body(...), user: dict = Depends(auth.current_user)):
    name = " ".join(str(body.get("name") or "").split())
    if not 1 <= len(name) <= 60:
        raise HTTPException(400, "A folder name needs 1 to 60 characters.")
    if db.count_folders(user["id"]) >= config.MAX_FOLDERS_PER_USER:
        raise HTTPException(429, f"You can have up to {config.MAX_FOLDERS_PER_USER} folders.")
    folder_id = db.create_folder(user["id"], name)
    if folder_id is None:
        raise HTTPException(409, f"You already have a folder called '{name}'.")
    logger.info("folder {} created: {!r}", folder_id, name)
    return dict(id=folder_id, name=name)


@app.post("/api/runs/{run_id}/folder")
def file_run(run_id: str, body: dict = Body(...), user: dict = Depends(auth.current_user)):
    """Put a decided invoice in one of this account's folders, or take it out with folder_id null."""
    row = db.get_run(run_id, user["id"])
    if not row or row["status"] not in ("done", "error"):
        raise HTTPException(404, "run not found or not decided yet")
    folder_id = body.get("folder_id")
    if folder_id is not None and not (isinstance(folder_id, int) and db.get_folder(folder_id, user["id"])):
        raise HTTPException(404, "folder not found")
    if folder_id is not None and (row["final_decision"] or row["decision"]) == "REJECT":
        raise HTTPException(400, "A rejected invoice can't be added to a folder.")
    db.update_run(run_id, folder_id=folder_id)
    filed = []
    if folder_id is not None and row["batch_id"]:  # the rest of its folder goes along, if decided, not rejected and not filed
        for r in db.list_runs(user["id"], limit=-1):
            if (r["batch_id"] == row["batch_id"] and r["id"] != run_id and r["status"] == "done" and r["folder_id"] is None
                    and (r["final_decision"] or r["decision"]) != "REJECT"):
                db.update_run(r["id"], folder_id=folder_id)
                filed.append(r["id"])
    return dict(ok=True, folder_id=folder_id, filed=filed)


@app.post("/api/batches/{batch_id}/folder")
def file_batch(batch_id: str, body: dict = Body(...), user: dict = Depends(auth.current_user)):
    """Put every checked invoice from one folder upload into one of this account's folders. Rejected ones can't be filed,
    and ones still being checked are left out (the reply says how many)."""
    folder_id = body.get("folder_id")
    if not (isinstance(folder_id, int) and db.get_folder(folder_id, user["id"])):
        raise HTTPException(404, "folder not found")
    runs = [r for r in db.list_runs(user["id"], limit=-1) if r["batch_id"] == batch_id]
    if not runs:
        raise HTTPException(404, "batch not found")
    filed = [r["id"] for r in runs if r["status"] in ("done", "error") and (r["final_decision"] or r["decision"]) != "REJECT"]
    for rid in filed:
        db.update_run(rid, folder_id=folder_id)
    rejected = sum(r["status"] in ("done", "error") and (r["final_decision"] or r["decision"]) == "REJECT" for r in runs)
    logger.info("folder upload {}: {} invoice(s) saved to folder {}", batch_id, len(filed), folder_id)
    return dict(ok=True, filed=filed, rejected=rejected, unfinished=len(runs) - len(filed) - rejected)


@app.post("/api/runs/{run_id}/cancel")
def cancel_run(run_id: str, user: dict = Depends(auth.current_user)):
    """Stop a document that hasn't been decided and discard it. Waiting (queued or at Review): gone at once.
    Being read or checked: it stops when the current stage ends, because a stage can't be interrupted safely."""
    row = db.get_run(run_id, user["id"])
    if not row:
        raise HTTPException(404, "run not found")
    if row["status"] not in ("queued", "running", "review", "waiting"):
        raise HTTPException(409, "This invoice has already been decided. To remove it, delete it from History.")
    logger.bind(run=run_id).info("cancel requested ({})", row["status"])
    if row["status"] == "running":
        CANCELLED.add(run_id)
        return dict(ok=True, stopping=True)
    CANCELLED.add(run_id)  # in case the worker picks it up this very moment
    db.delete_run(run_id, user["id"])
    bus = BUS.pop(run_id, None)
    if bus:
        bus["done"] = True  # ends any live stream that is waiting on it
    Path(row["file_path"]).unlink(missing_ok=True)
    if row["status"] == "review" or (row["status"] == "queued" and row["batch_id"] and db.batch_waiting(row["batch_id"], user["id"])):
        _promote_batch(row["batch_id"], user["id"])  # a folder's first invoice: the next one asks for the approval
    return dict(ok=True, stopping=False)


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str, user: dict = Depends(auth.current_user)):
    """Remove an invoice from history for good: record, file and PO posting. The same file uploaded again is new."""
    row = db.get_run(run_id, user["id"])
    if not row:
        raise HTTPException(404, "run not found")
    if row["status"] in ("queued", "running"):
        raise HTTPException(409, "This invoice is still being processed. Delete it once it has finished.")
    path = db.delete_run(run_id, user["id"])
    BUS.pop(run_id, None)
    Path(path).unlink(missing_ok=True)
    return dict(ok=True)


@app.post("/api/folders/{folder_id}/reset")
def reset_folder(folder_id: int, user: dict = Depends(auth.current_user)):
    """Delete the invoices in one folder, with their files and PO postings. The folder stays, empty."""
    if not db.get_folder(folder_id, user["id"]):
        raise HTTPException(404, "folder not found")
    removed = db.reset_folder(folder_id, user["id"])
    for run_id, path in removed:
        BUS.pop(run_id, None)
        Path(path).unlink(missing_ok=True)
    return dict(ok=True, removed=[run_id for run_id, _ in removed])


@app.get("/api/pos")
def pos(user: dict = Depends(auth.current_user)):
    vendors = {v["vendor_id"]: v for v in rules.load_vendors(user["id"])}
    out = []
    for p in rules.load_pos():
        billed = db.billed(p["po_number"], user["id"])
        out.append(dict(p, vendor_name=vendors[p["vendor_id"]]["name"], billed=round(billed, 2),
                        remaining=round(p["net_amount"] - billed, 2), pct_billed=round(billed / p["net_amount"] * 100, 1),
                        invoices=db.ledger_for(p["po_number"], user["id"])))
    return dict(pos=out, vendors=list(vendors.values()))


def _stats(rows):
    """Dashboard figures. Rejected documents are left out: they stay on the Process tab and in the database."""
    eff = lambda r: r["final_decision"] or r["decision"]
    rs = [r for r in rows if r["status"] == "done" and eff(r) != "REJECT"]
    n = len(rs)
    counts = {k: sum(1 for r in rs if eff(r) == k) for k in ("APPROVE", "HOLD")}
    auto = sum(1 for r in rs if r["decision"] == "APPROVE")
    reasons = {}
    for r in rs:
        for c in set(r["codes"]):
            reasons[c] = reasons.get(c, 0) + 1
    return dict(
        total=n, counts=counts, straight_through=round(auto / n * 100) if n else 0,
        approved_value=round(sum(r["total"] or 0 for r in rs if eff(r) == "APPROVE"), 2),
        pending_value=round(sum(r["total"] or 0 for r in rs if eff(r) == "HOLD"), 2),
        avg_seconds=round(sum(r["duration_ms"] or 0 for r in rs) / n / 1000, 1) if n else 0,
        reasons=sorted(({"code": k, "count": v} for k, v in reasons.items()), key=lambda x: -x["count"])[:8],
        queued=sum(1 for r in rows if r["status"] in ("queued", "running")))


def _in_period(rows, period):
    """Runs whose invoice date falls in a year (2026) or month (2026-10). Blank means all of them."""
    if not period:
        return rows
    if not re.fullmatch(r"\d{4}(-\d{2})?", period):
        raise HTTPException(400, "period must look like 2026 or 2026-10")
    return [r for r in rows if (r["invoice_date"] or "").startswith(period)]


def _scope(user, period, folder):
    """This account's runs, narrowed to a period and to one folder when asked."""
    rows = _in_period(db.list_runs(user["id"], limit=-1), period)
    return [r for r in rows if r["folder_id"] == folder] if folder is not None else rows


@app.get("/api/stats")
def stats(user: dict = Depends(auth.current_user), period: str = "", folder: int | None = None):
    return _stats(_scope(user, period, folder))


HOLD_AGES = [("Under a day", 0, 1), ("1-3 days", 1, 3), ("3-7 days", 3, 7), ("Over a week", 7, None)]


def _analytics(rows, now=None):
    """Figures for the Analytics tab. Unlike the Overview, rejected invoices count: several charts are about them."""
    now = now or datetime.now()
    rows = [r for r in rows if r["status"] in ("done", "error") and r["decision"]]
    eff = lambda r: r["final_decision"] or r["decision"]
    received = lambda r: datetime.fromisoformat(r["created_at"])
    money = lambda rs: round(sum(r["total"] or 0 for r in rs), 2)

    # what needs a person now: count and value by outcome, and the money on hold by how long it has waited
    outcome = {k: dict(count=len(rs), value=money(rs)) for k in ("APPROVE", "HOLD", "REJECT") for rs in [[r for r in rows if eff(r) == k]]}
    held, ages = [r for r in rows if eff(r) == "HOLD"], []
    for label, lo, hi in HOLD_AGES:
        rs = [r for r in held if lo <= (now - received(r)).total_seconds() / 86400 < (hi or float("inf"))]
        ages.append(dict(label=label, count=len(rs), value=money(rs)))

    # money approved for payment, by invoice date (the page rolls days up into months or years)
    spend, undated = {}, 0
    for r in rows:
        if eff(r) == "APPROVE":
            if r["invoice_date"]:
                spend[r["invoice_date"]] = round(spend.get(r["invoice_date"], 0) + (r["total"] or 0), 2)
            else:
                undated += 1

    # fraud and control flags, counted apart from the holds
    flags = {kind: [dict(code=c, count=n) for c in codes for n in [sum(1 for r in rows if c in r["flags"])] if n]
             for kind, codes in rules.FLAG_KIND.items()}

    # per vendor: how much it sends, how often it stops, and why
    by_vendor = {}
    for r in rows:
        key = r["vendor_id"] or (r["vendor_name"] or "").strip().lower() or None
        if key:
            by_vendor.setdefault(key, []).append(r)
    vendors = []
    for rs in by_vendor.values():
        stopped = [r for r in rs if r["decision"] != "APPROVE"]
        reasons = Counter(r["codes"][0] for r in stopped if r["codes"])
        vendors.append(dict(name=next((r["vendor_name"] for r in rs if r["vendor_name"]), "Unknown"), invoices=len(rs), stopped=len(stopped),
                            hold_rate=round(len(stopped) / len(rs) * 100), value=money(rs),
                            top_reason=reasons.most_common(1)[0][0] if reasons else None,
                            top_reason_count=reasons.most_common(1)[0][1] if reasons else 0))
    vendors.sort(key=lambda v: (-v["invoices"], -v["hold_rate"], v["name"]))

    # throughput per upload: a batch is a folder or several files uploaded together; single files are one row
    groups = {}
    for r in rows:
        groups.setdefault(r["batch_id"] or "single", []).append(r)
    batches = []
    for key, rs in groups.items():
        rs.sort(key=lambda r: r["created_at"])
        cleared = sum(1 for r in rs if r["decision"] == "APPROVE" and not r["final_decision"])
        batches.append(dict(batch=None if key == "single" else key, first=rs[0]["filename"], started=rs[0]["created_at"], invoices=len(rs),
                            avg_seconds=round(sum(r["duration_ms"] or 0 for r in rs) / len(rs) / 1000, 1),
                            cleared=cleared, cleared_pct=round(cleared / len(rs) * 100)))
    batches = sorted([b for b in batches if b["batch"]], key=lambda b: b["started"], reverse=True) + [b for b in batches if not b["batch"]]

    return dict(total=len(rows), outcome=outcome, hold_ages=ages, spend=sorted(spend.items()), undated_spend=undated,
                flags=flags, vendors=vendors[:15], vendor_count=len(vendors), batches=batches[:12])


@app.get("/api/analytics")
def analytics(user: dict = Depends(auth.current_user), period: str = "", folder: int | None = None):
    return _analytics(_scope(user, period, folder))


@app.get("/api/export.xlsx")
def export_xlsx(user: dict = Depends(auth.current_user), period: str = "", folder: int | None = None):
    rows = _scope(user, period, folder)
    data = export.workbook(_stats(rows), rows, pos(user)["pos"], rules.load_vendors(user["id"]), period)
    folder_name = (db.get_folder(folder, user["id"]) or {}).get("name") if folder is not None else None
    slug = re.sub(r"[^A-Za-z0-9]+", "-", folder_name).strip("-").lower() + "-" if folder_name else ""
    name = f"invoice-flow-report-{slug}{period or config.AS_OF}.xlsx"
    return Response(data, media_type=XLSX, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/vendors.xlsx")
def vendors_xlsx(user: dict = Depends(auth.current_user)):
    """The approved vendor list on its own, for this account."""
    data = export.approved_vendors(rules.load_vendors(user["id"]), db.list_runs(user["id"], limit=-1))
    name = f"approved-vendors-{config.AS_OF}.xlsx"
    return Response(data, media_type=XLSX, headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.get("/api/config")
def cfg(user: dict = Depends(auth.current_user)):
    return dict(rules=config.RULES, as_of=config.AS_OF, buyer=config.BUYER_NAME, labels=rules.CODE_LABEL,
                extractor=dict(mode=config.EXTRACTOR, llm=extract.llm_available(), provider=extract.llm_provider()[0], model=extract.llm_provider()[1],
                               ocr=extract.ocr_available(), key_rejected=bool(extract.llm_key_problem())),
                stages=[dict(id=i, title=t) for i, t in pipeline.STAGES])


@app.get("/api/samples")
def samples(user: dict = Depends(auth.current_user)):
    return dict(samples=json.loads((config.SAMPLES_DIR / "synthetic" / "manifest.json").read_text()))


@app.get("/")
def index():
    """The page, with each script and stylesheet link stamped with that file's last change, so a browser never runs
    yesterday's app.js against today's page (the page itself is always re-checked)."""
    html = (config.STATIC_DIR / "index.html").read_text(encoding="utf-8")
    for name in ("app.css", "app.js", "theme.js"):
        html = html.replace(f"/static/{name}\"", f"/static/{name}?v={int((config.STATIC_DIR / name).stat().st_mtime)}\"")
    provider = extract.llm_provider()[0]
    if provider:  # say so before anyone signs up: their documents leave this server
        who = "Google Gemini" if provider == "Gemini" else "Anthropic's Claude"
        html = html.replace("are private to your account.</p>", f"are private to your account.</p>\n  <p class=\"auth-note\">Documents you upload are "
                            f"sent to {who} to be read. Don't upload anything you aren't allowed to share with them.</p>", 1)
    return Response(html, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")
_resume_stuck_batches()  # last, once everything it calls is defined
