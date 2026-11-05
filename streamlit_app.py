"""Invoice Flow on Streamlit: the same pipeline, rules, accounts and folders as `python run.py`, with a Streamlit UI.

The FastAPI app in app/main.py is imported, not served: its route functions are plain Python functions that take the
signed-in user, so this page calls them directly, exactly as tools/build_demo.py does. Its single background worker
(started when app.main is imported) reads and checks every document, so results match the original app.

Streamlit Community Cloud keeps no files between restarts: accounts, folders and invoices start empty after one.
"""
import asyncio
import io
import json
import os
import time
from pathlib import Path

import streamlit as st

st.set_page_config(page_title="Invoice Flow", page_icon="✅", layout="wide", initial_sidebar_state="expanded")

# Streamlit secrets (GEMINI_API_KEY, ...) become environment variables before the app's config is read
try:
    for _k, _v in st.secrets.items():
        if isinstance(_v, (str, int, float, bool)):
            os.environ.setdefault(_k, str(_v))
except Exception:  # no secrets file: run with the built-in rules and OCR
    pass
os.environ.setdefault("COOKIE_SECURE", "1")
for _k, _v in dict(MAX_QUEUED_PER_USER="25", MAX_RUNS_PER_USER="1000", MAX_UPLOAD_MB="25", GEMINI_DAILY_LIMIT="300").items():
    os.environ.setdefault(_k, _v)


@st.cache_resource(show_spinner="Starting Invoice Flow…")
def backend():
    """Imported once per server process: one database, one queue and one worker, shared by every visitor."""
    from fastapi import HTTPException
    from starlette.datastructures import UploadFile

    from app import auth, config, db, extract, main, pipeline, rules
    return dict(main=main, db=db, auth=auth, rules=rules, extract=extract, config=config, pipeline=pipeline,
                HTTPException=HTTPException, UploadFile=UploadFile)


B = backend()
main, db, auth, rules, extract, config = B["main"], B["db"], B["auth"], B["rules"], B["extract"], B["config"]
HTTPException = B["HTTPException"]

# ------------------------------------------------------------------------------------------------ look and feel --
st.markdown("""
<style>
@import url('https://fonts.googleapis.com/css2?family=Public+Sans:wght@400;500;600;700;800&display=swap');
html, body, [class*="css"], .stMarkdown, .stButton button, input, textarea, select { font-family: "Public Sans", system-ui, sans-serif; }
h1, h2, h3 { color: #1F7A63; font-weight: 800 !important; letter-spacing: -0.01em; }
.badge { display:inline-block; padding:2px 10px; border-radius:999px; font-weight:700; font-size:.8rem; letter-spacing:.04em; border:1.5px solid; }
.b-APPROVE { color:#1E7A4C; border-color:#1E7A4C; background:#E3F4EA; }
.b-HOLD { color:#9A5B08; border-color:#9A5B08; background:#FCF0DC; }
.b-REJECT { color:#B3261E; border-color:#B3261E; background:#FBE5E2; }
.b-WAIT { color:#4B5B68; border-color:#9AA3A8; background:#F5F7FA; }
.stage { padding:6px 0; border-bottom:1px solid #ECF0F3; }
.stage small { color:#4B5B68; }
.muted { color:#4B5B68; }
.brand { font-size:1.35rem; font-weight:800; color:#081C2D; display:flex; align-items:center; gap:.5rem; }
section[data-testid="stSidebar"] .brand { color:#FFFFFF; }
section[data-testid="stSidebar"] .brand span.logo { background:#081C2D; }
.brand span.logo { background:linear-gradient(135deg,#24896F,#145443); color:white; border-radius:10px; width:34px; height:34px;
  display:inline-flex; align-items:center; justify-content:center; font-size:1.1rem; }
</style>
""", unsafe_allow_html=True)

DECISION_WORD = {"APPROVE": "Approved", "HOLD": "Held for review", "REJECT": "Rejected"}
STATUS_ICON = {"running": "⏳", "ok": "✅", "info": "ℹ️", "warn": "⚠️", "hold": "✋", "reject": "⛔", "skip": "⏭️", "error": "⛔"}
SEVERITY_ICON = {"info": "ℹ️", "warn": "⚠️", "hold": "✋", "reject": "⛔"}
READABLE = ["pdf", "png", "jpg", "jpeg", "webp", "tif", "tiff", "bmp", "zip"]


def badge(decision, final=None):
    d = final or decision
    if not d:
        return '<span class="badge b-WAIT">WAITING</span>'
    label = {"APPROVE": "APPROVED", "HOLD": "HELD", "REJECT": "REJECTED"}[d]
    return f'<span class="badge b-{d}">{label}{" · by a person" if final else ""}</span>'


def money(v):
    if v is None:
        return "–"
    v = round(float(v), 2)
    whole, frac = f"{v:.2f}".split(".")
    neg = whole.startswith("-")
    whole = whole.lstrip("-")
    if len(whole) > 3:  # Indian grouping: 12,34,567
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return f"{'-' if neg else ''}₹{whole}.{frac}"


def call(fn, *args, **kwargs):
    """Run one of the app's route functions; show its refusal (HTTPException) instead of crashing."""
    try:
        return fn(*args, **kwargs)
    except HTTPException as e:
        st.error(e.detail)
    return None


def cfg():
    return main.cfg(USER)


# ------------------------------------------------------------------------------------------------------ sign in --
SSO_PREFIX = "sso$google$"  # stored instead of a password hash: such an account can only sign in with Google


def sso_configured():
    """Google sign-in shows once the app's secrets have an [auth] section with a [auth.google] provider."""
    try:
        return "auth" in st.secrets and "google" in st.secrets["auth"]
    except Exception:
        return False


def sso_user():
    """The Invoice Flow account for whoever signed in with Google, created on their first visit. None if not signed in."""
    if not sso_configured() or not getattr(st.user, "is_logged_in", False):
        return None
    email = str(st.user.get("email") or "").strip().lower()
    if not email or st.user.get("email_verified") is False:
        st.error("Your Google account didn't share a verified email address, so it can't be used here.")
        return None
    if not auth.USERNAME_RE.fullmatch(email):
        st.error("This email address can't be used as an Invoice Flow account name. Create an account with a username instead.")
        return None
    user = db.find_user(email)
    if user is None:
        uid = db.create_user(email, SSO_PREFIX + os.urandom(16).hex())
        user = db.find_user(email) if uid is None else dict(id=uid, username=email, pw_hash=SSO_PREFIX)
    if not user["pw_hash"].startswith(SSO_PREFIX):
        # a password account already uses this name: never hand it to a Google login, or anyone could pre-register a
        # victim's email as a username and read whatever that person later uploads
        st.error(f"An account called {email} already exists with a password. Log in with its password instead.")
        return None
    return dict(id=user["id"], username=user["username"], sso=True)


def sign_in_page():
    _, mid, _ = st.columns([1, 1.3, 1])
    with mid:
        st.markdown('<div class="brand"><span class="logo">✓</span>Invoice Flow</div>', unsafe_allow_html=True)
        st.write("Upload an invoice. It's read, checked against your purchase orders and approved vendors, and you get "
                 "**Approve, Hold or Reject** with the reasons.")
        provider = extract.llm_provider()[0]
        if sso_configured():
            if getattr(st.user, "is_logged_in", False):  # signed in with Google but refused above: offer a way out
                if st.button("Sign out of Google", use_container_width=True):
                    st.logout()
            elif st.button("Continue with Google", type="primary", use_container_width=True, icon=":material/login:"):
                st.login("google")
            st.markdown("<p class='muted' style='text-align:center'>or use a username and password</p>", unsafe_allow_html=True)
        tab_in, tab_up = st.tabs(["Log in", "Create account"])
        with tab_in, st.form("login"):
            name = st.text_input("Username")
            pw = st.text_input("Password", type="password")
            if st.form_submit_button("Log in", type="primary", use_container_width=True):
                key = f"name:{name.strip().lower()}"
                if auth._too_many(key, 5, 900):
                    st.error("Too many failed attempts. Try again in 15 minutes.")
                else:
                    user = db.find_user(name.strip())
                    if user and user["pw_hash"].startswith(SSO_PREFIX):
                        st.error("This account signs in with Google. Use Continue with Google.")
                        st.stop()
                    if user and auth.verify_password(pw[:200], user["pw_hash"]):
                        st.session_state.user = dict(id=user["id"], username=user["username"])
                        st.rerun()
                    auth._record(key)
                    st.error("Wrong username or password.")
        with tab_up, st.form("signup"):
            name = st.text_input("Choose a username", help="3 to 64 letters, numbers or . _ @ + -")
            pw = st.text_input("Choose a password", type="password", help="At least 8 characters")
            if st.form_submit_button("Create account", type="primary", use_container_width=True):
                name = name.strip()
                if not config.ALLOW_SIGNUP:
                    st.error("New accounts are switched off.")
                elif not auth.USERNAME_RE.fullmatch(name):
                    st.error("Usernames are 3 to 64 letters, numbers or . _ @ + -")
                elif not 8 <= len(pw) <= 200:
                    st.error("Passwords need at least 8 characters.")
                else:
                    uid = db.create_user(name, auth.hash_password(pw))
                    if uid is None:
                        st.error("That username is taken.")
                    else:
                        st.session_state.user = dict(id=uid, username=name)
                        st.rerun()
        st.caption("Each account has its own invoices, folders, PO ledger and approved vendors, which are private to it.")
        if provider:
            st.caption(f"Documents you upload are sent to {'Google Gemini' if provider == 'Gemini' else 'Anthropic’s Claude'} to be read. "
                       "Don't upload anything you aren't allowed to share with them.")
        st.caption("This hosted copy starts empty after the server restarts: accounts and invoices aren't kept for good.")


if "user" not in st.session_state:
    _sso = sso_user()
    if _sso:
        st.session_state.user = _sso
    else:
        sign_in_page()
        st.stop()

USER = st.session_state.user
CFG = cfg()
LABEL = CFG["labels"]
STAGES = CFG["stages"]
ss = st.session_state
ss.setdefault("page", "Process")
ss.setdefault("current", None)     # run id on screen in Process
ss.setdefault("watch", [])         # run ids being processed, in order
ss.setdefault("folder_view", None)  # folder id whose Dashboard is open


def folders():
    return main.folders(USER)["folders"]


def folder_name(fid):
    return next((f["name"] for f in folders() if f["id"] == fid), None)


# -------------------------------------------------------------------------------------------------------- sidebar --
with st.sidebar:
    st.markdown('<div class="brand"><span class="logo">✓</span>Invoice Flow</div>', unsafe_allow_html=True)
    st.write("")
    ss.page = st.radio("Go to", ["Process", "Dashboard", "PO ledger", "Rules"], index=["Process", "Dashboard", "PO ledger", "Rules"].index(ss.page),
                       label_visibility="collapsed")
    st.markdown("**Folders**")
    with st.popover("New folder", icon=":material/create_new_folder:", use_container_width=True):
        new_name = st.text_input("Folder name", key="new_folder_name")
        if st.button("Create", key="create_folder", type="primary"):
            made = call(main.create_folder, dict(name=new_name), USER)
            if made:
                st.toast(f"Folder {made['name']} created.")
                st.rerun()
    for f in folders():
        c1, c2 = st.columns([5, 1])
        if c1.button(f"🗂️ {f['name']}  ·  {f.get('count', 0)}", key=f"fo{f['id']}", use_container_width=True):
            ss.folder_view, ss.page = f["id"], "Dashboard"
            st.rerun()
        with c2.popover("↺"):
            st.write(f"Delete the invoices in **{f['name']}**, with their files and PO postings? The folder stays.")
            if st.button("Reset folder", key=f"rf{f['id']}", type="primary"):
                call(main.reset_folder, f["id"], USER)
                st.rerun()
    st.divider()
    e = CFG["extractor"]
    reader = f"**{'Google Gemini' if e['provider'] == 'Gemini' else e['provider']}** ({e['model']})" if e["llm"] and e["mode"] != "rules" else "**built-in rules**"
    st.caption(f"Reading with {reader}{' and OCR' if e['ocr'] else ''}  \nDates checked as of {CFG['as_of']}")
    c1, c2 = st.columns([4, 1])
    c1.markdown(f"**{USER['username']}**")
    if c2.button("⎋", help="Log out"):
        signed_in_with_google = USER.get("sso")
        for k in list(ss.keys()):
            del ss[k]
        if signed_in_with_google:
            st.logout()  # also ends the Google sign-in, or the next page load would sign straight back in
        st.rerun()


# ----------------------------------------------------------------------------------------------- live progress --
def stage_lines(events):
    last = {}
    for ev in events:
        last[ev["stage"]] = ev
    out = []
    for s in STAGES:
        ev = last.get(s["id"])
        if not ev:
            out.append(f'<div class="stage">◯ <span class="muted">{s["title"]}</span></div>')
        else:
            icon = STATUS_ICON.get(ev["status"], "•")
            summary = f'<br><small>{ev["summary"]}</small>' if ev.get("summary") else ""
            out.append(f'<div class="stage">{icon} <b>{ev.get("title") or s["title"]}</b>{summary}</div>')
    return "\n".join(out)


def watch(run_id, label=""):
    """Show the stages as the worker reaches them, until the run stops at Review or is decided."""
    box = st.empty()
    deadline = time.time() + 900
    while time.time() < deadline:
        row = db.get_run(run_id, USER["id"])
        if not row:
            box.warning("This document was cancelled.")
            return None
        bus = main.BUS.get(run_id)
        events = bus["events"] if bus else json.loads(row.get("events_json") or "[]")
        with box.container(border=True):
            st.markdown(f"**{label}{row['filename']}**  ·  " + ("waiting in the queue" if row["status"] == "queued" else row["status"]))
            st.markdown(stage_lines(events), unsafe_allow_html=True)
        finished = row["status"] in ("review", "done", "error") and (bus is None or bus["done"])
        if finished:
            return row["status"]
        time.sleep(0.4)
    box.warning("Still working. Refresh the page to see where it got to.")
    return None


def run_queue():
    """Process the documents in ss.watch one after another on screen; a review stops the queue for a person."""
    while ss.watch:
        rid = ss.watch[0]
        n_left = len(ss.watch)
        status = watch(rid, f"{n_left} to go · " if n_left > 1 else "")
        ss.watch.pop(0)
        ss.current = rid
        if status == "review":
            break
    st.rerun()


# ----------------------------------------------------------------------------------------------------- process --
def preview(row_id):
    row = db.get_run(row_id, USER["id"])
    if not row or not Path(row["file_path"]).exists():
        return
    p = Path(row["file_path"])
    if p.suffix.lower() == ".pdf":
        st.image(main.run_preview(row_id, USER).body, use_container_width=True)
    else:
        st.image(str(p), use_container_width=True)
    st.download_button("Download the original", p.read_bytes(), file_name=row["filename"], key=f"dl{row_id}", use_container_width=True)


def new_document():
    st.header("New document")
    st.write("Add an invoice or bill. You'll watch it being read, check what was found, then get a decision.")
    files = st.file_uploader("Choose files or drop them here: PDFs or photos, one, several, or a ZIP", type=READABLE, accept_multiple_files=True)
    if files:
        st.subheader("Approvals")
        st.caption("Choose what can be approved without asking you, for every invoice in this upload. Every other check still runs.")
        a_vendors = st.checkbox("Auto-approve all the vendors", help="A vendor that isn't on your approved list is added when its invoice is "
                                "checked. A name resembling a vendor on file or a blocked one, or a GSTIN that disagrees, is still held.")
        a_po = st.checkbox("Auto-approve all the PO references, whether present or not", help="An invoice citing a PO number that isn't in your "
                           "register, or citing none, isn't held for it. A registered PO is still checked against its balance.")
        opts = {None: "Don't file"} | {f["id"]: f["name"] for f in folders()}
        a_folder = st.selectbox("Add all the invoices to a folder (optional)", list(opts), format_func=lambda k: opts[k])
        if st.button("Start processing →", type="primary"):
            ups = [B["UploadFile"](file=io.BytesIO(f.getvalue()), filename=f.name) for f in files]
            approvals = json.dumps(dict(vendors=a_vendors, po=a_po, folder_id=a_folder))
            try:
                out = asyncio.run(main.upload(files=ups, inputs="{}", batch="1" if len(files) > 1 else "", approvals=approvals, user=USER))
            except HTTPException as e:
                st.error(e.detail)
                return
            first = [r["id"] for r in out["runs"] if not r["waiting"]]
            ss.batch = [r["id"] for r in out["runs"]]
            ss.watch = first
            ss.current = first[0] if first else None
            st.rerun()

    st.divider()
    st.subheader("Or try a sample")
    st.caption("Each builds on the last, so run them in order for the full story (use Reset on the Dashboard first to start over).")
    samples = main.samples(USER)["samples"]
    if st.button("Run all in order", type="primary"):
        out = call(main.run_samples, dict(files=[s["file"] for s in samples]), USER)
        if out:
            ss.watch = [r["id"] for r in out["runs"]]
            ss.batch = []
            st.rerun()
    runs = {r["filename"]: r for r in reversed(db.list_runs(USER["id"], limit=-1))}
    for s in samples:
        c1, c2, c3 = st.columns([0.5, 7, 2])
        c1.markdown(f"**{s['order']}**")
        c2.markdown(f"{s['title']}  \n<small class='muted'>Expected: {DECISION_WORD[s['expected']].lower()}</small>", unsafe_allow_html=True)
        done = runs.get(s["file"])
        if done and done["status"] == "done":
            if c3.button("Show result", key=f"show{s['file']}", use_container_width=True):
                ss.current = done["id"]
                st.rerun()
        elif c3.button("Run", key=f"run{s['file']}", use_container_width=True):
            out = call(main.run_samples, dict(files=[s["file"]]), USER)
            if out:
                ss.watch = [out["runs"][0]["id"]]
                ss.batch = []
                st.rerun()


def review_page(run_id):
    row = db.get_run(run_id, USER["id"])
    read = json.loads(db.run_stage(run_id))["fields"]
    st.header("Review details")
    st.write("Check what was read. Fill a gap or correct a value; a value that disagrees with the document holds the invoice for a person.")
    left, right = st.columns([1, 1.4])
    with left:
        preview(run_id)
    with right:
        with st.form("review"):
            typed = {}
            for key, label in extract.USER_FIELDS.items():
                shown = "" if read.get(key) is None else str(read.get(key))
                typed[key] = (st.text_input(label, value=shown, key=f"rv_{run_id}_{key}"), shown)
            note = st.text_input("Note (optional)", key=f"rv_{run_id}_note")
            name, gstin = typed["vendor_name"][0].strip(), typed["vendor_gstin"][0].strip().upper()
            match = rules.match_vendor(dict(vendor_name=name, vendor_gstin=gstin), rules.load_vendors(USER["id"]))[0]
            inputs = (json.loads(row["inputs_json"] or "{}").get("batch") or {})
            approve = no_po = False
            if match["status"] == "approved":
                st.success(f"Vendor on your approved list: {match['vendor']['name']}")
            elif inputs.get("all_vendors"):
                st.info("New vendors in this upload are approved as their invoices are checked.")
            else:
                st.warning("This vendor isn't on your approved list.")
                approve = st.checkbox("Approve this vendor", key=f"rv_{run_id}_approve")
                no_po = st.checkbox("This vendor doesn't use purchase orders", key=f"rv_{run_id}_nopo")
            all_vendors = batch_po = None
            if row["batch_id"]:
                st.markdown("**For the rest of this upload**")
                all_vendors = st.checkbox("Approve all the new vendors in this upload", key=f"rv_{run_id}_all")
                po_pick = st.radio("PO numbers its invoices cite", ["Check them as usual", "Accept them (not in the register)",
                                                                    "This vendor doesn't use POs"], key=f"rv_{run_id}_bpo")
                batch_po = {"Accept them (not in the register)": "accept", "This vendor doesn't use POs": "no_po"}.get(po_pick)
            if st.form_submit_button("Run the checks →", type="primary"):
                body = dict(inputs={k: v for k, (v, was) in typed.items() if v.strip() != was.strip()} | ({"note": note} if note.strip() else {}))
                if approve:
                    body["approve_vendor"] = dict(note=note, no_po=no_po)
                if all_vendors:
                    body["approve_all_vendors"] = True
                if batch_po:
                    body["batch_po"] = batch_po
                out = call(main.continue_run, run_id, body, USER)
                if out:
                    ss.watch = [run_id] + [r["id"] for r in out.get("batch", [])]
                    st.rerun()
    if st.button("Cancel this document"):
        call(main.cancel_run, run_id, USER)
        ss.current = None
        st.rerun()


def decision_view(run_id, key=""):
    d = call(main.run_detail, run_id, USER)
    if not d:
        return
    res = d.get("result") or {}
    final, decision = d.get("final_decision"), d.get("decision")
    st.markdown(f"### {d['filename']}  {badge(decision, final)}", unsafe_allow_html=True)
    if res.get("headline"):
        (st.success if (final or decision) == "APPROVE" else st.warning if (final or decision) == "HOLD" else st.error)(res["headline"])
    if final and d.get("override_note"):
        st.caption(f"Note from the person who decided: {d['override_note']}")
    if res.get("learned"):
        st.info("Remembered from this approval: " + "; ".join(map(str, res["learned"])))

    left, right = st.columns([1, 1.5])
    with left:
        preview(run_id)
    with right:
        findings = res.get("findings") or []
        stops = [f for f in findings if f["severity"] in ("hold", "reject")]
        notes = [f for f in findings if f["severity"] in ("warn", "info")]
        if stops:
            st.markdown("**What stopped it**")
            for f in stops:
                st.markdown(f"{SEVERITY_ICON[f['severity']]} {f['message']}  \n<small class='muted'>{LABEL.get(f['code'], f['code'])}</small>",
                            unsafe_allow_html=True)
        if notes:
            with st.expander(f"{len(notes)} other note{'s' if len(notes) != 1 else ''} from the checks"):
                for f in notes:
                    st.markdown(f"{SEVERITY_ICON[f['severity']]} {f['message']}")
        if res.get("next_steps"):
            st.markdown("**What happens next**")
            for step in res["next_steps"]:
                st.markdown(f"- {step}")
        msg = res.get("message")
        if msg:
            with st.expander(f"Draft message to {msg.get('audience', 'the vendor')}"):
                st.text_input("Subject", msg.get("subject", ""), key=f"subj{key}{run_id}")
                st.text_area("Message", msg.get("body", ""), height=240, key=f"body{key}{run_id}")

        codes = {f["code"] for f in findings}
        if decision == "HOLD" and not final:
            st.markdown("**Your decision**")
            if "VENDOR_UNKNOWN" in codes:
                with st.expander("Approve this vendor and check again"):
                    f = res.get("fields") or {}
                    vn = st.text_input("Vendor name", f.get("vendor_name") or "", key=f"avn{key}{run_id}")
                    vg = st.text_input("GSTIN", f.get("vendor_gstin") or "", key=f"avg{key}{run_id}")
                    vnote = st.text_input("Note", key=f"avnote{key}{run_id}")
                    vnopo = st.checkbox("This vendor doesn't use purchase orders", key=f"avnopo{key}{run_id}")
                    if st.button("Approve vendor", key=f"avb{key}{run_id}", type="primary"):
                        if call(main.approve_vendor, run_id, dict(name=vn, gstin=vg, note=vnote, no_po=vnopo), USER):
                            ss.watch, ss.current, ss.page = [run_id], run_id, "Process"
                            st.rerun()
            if codes & rules.PO_HOLDS and d.get("vendor_id"):
                with st.expander("Answer the PO question and check again"):
                    opts = d.get("po_options") or []
                    pick = st.selectbox("Match it to an open PO", [None] + [p["po_number"] for p in opts],
                                        format_func=lambda n: "Choose a PO" if n is None else
                                        next(f"{p['po_number']} · {p['description']} · {money(p['remaining'])} left" for p in opts if p["po_number"] == n),
                                        key=f"pop{key}{run_id}")
                    nopo = st.checkbox("This vendor doesn't use POs", key=f"ponp{key}{run_id}", disabled=not (codes & rules.NO_PO_WAIVES))
                    if st.button("Check again", key=f"pob{key}{run_id}", type="primary"):
                        body = dict(no_po=True) if nopo else dict(po_number=pick)
                        if call(main.answer_po, run_id, body, USER):
                            ss.watch, ss.current, ss.page = [run_id], run_id, "Process"
                            st.rerun()
            note = st.text_input("Note (optional)", key=f"ovn{key}{run_id}")
            c1, c2, c3 = st.columns(3)
            if c1.button("Reject", key=f"ovr{key}{run_id}", use_container_width=True):
                if call(main.override, run_id, dict(decision="REJECT", note=note), USER):
                    st.rerun()
            c2.button("Keep on hold", key=f"ovk{key}{run_id}", use_container_width=True)
            if c3.button("Approve", key=f"ova{key}{run_id}", type="primary", use_container_width=True):
                out = call(main.override, run_id, dict(decision="APPROVE", note=note), USER)
                if out:
                    if out.get("folder"):
                        st.toast(f"Saved in {folder_name(out['folder']) or 'its vendor’s folder'}.")
                    st.rerun()
        if "NOT_AN_INVOICE" in codes and (final or decision) == "REJECT":
            if st.button("Yes, check it as an invoice", key=f"nai{key}{run_id}"):
                if call(main.treat_as_invoice, run_id, USER):
                    ss.watch, ss.current, ss.page = [run_id], run_id, "Process"
                    st.rerun()

        if (final or decision) != "REJECT" and d["status"] == "done":
            opts = {None: "Not in a folder"} | {f["id"]: f["name"] for f in folders()}
            cur = d.get("folder_id") if d.get("folder_id") in opts else None
            c1, c2 = st.columns([3, 1])
            pick = c1.selectbox("Folder", list(opts), index=list(opts).index(cur), format_func=lambda k: opts[k], key=f"fold{key}{run_id}")
            if c2.button("Save", key=f"folds{key}{run_id}", use_container_width=True) and pick != cur:
                if call(main.file_run, run_id, dict(folder_id=pick), USER) is not None:
                    st.toast("Taken out of its folder." if pick is None else f"Saved in {opts[pick]}.")
                    st.rerun()

    with st.expander("What was read"):
        f = res.get("fields") or {}
        rows = [(k.replace("_", " ").capitalize(), "" if v is None else str(v)) for k, v in f.items() if not isinstance(v, (dict, list))]
        st.table({"Field": [r[0] for r in rows], "Value": [r[1] for r in rows]})
        if res.get("line_items"):
            st.markdown("**Line items**")
            st.dataframe(res["line_items"], use_container_width=True, hide_index=True)
        st.caption(f"Read by {res.get('extractor', d.get('extractor'))} · {str(res.get('read_method', '')).replace('_', ' ')}")
    if res.get("checks"):
        with st.expander("Checks"):
            for c in res["checks"]:
                st.markdown(f"{'✅' if c['ok'] else '❌'} **{c['name']}**  \n<small class='muted'>{c.get('detail', '')}</small>", unsafe_allow_html=True)
    if res.get("budget"):
        b = res["budget"]
        with st.expander(f"PO {b['po_number']}: billing against the PO"):
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("PO value (pre-tax)", money(b["po_net"]))
            c2.metric("Billed before", money(b["billed_before"]))
            c3.metric("This invoice", money(b["this_net"]))
            c4.metric("Billed in all", f"{b['pct_billed']}%")
            st.progress(min(b["pct_billed"] / 100, 1.0))
    with st.expander("Every step"):
        st.markdown(stage_lines(d.get("events") or []), unsafe_allow_html=True)


def process_page():
    if ss.watch:
        st.header("Reading and checking")
        run_queue()
        return
    rid = ss.current
    row = db.get_run(rid, USER["id"]) if rid else None
    if row:
        if st.button("← New document"):
            ss.current = None
            st.rerun()
        if row["status"] == "review":
            review_page(rid)
        elif row["status"] in ("queued", "running"):
            ss.watch = [rid]
            st.rerun()
        elif row["status"] == "waiting":
            st.info(f"{row['filename']} waits for the first invoice of its upload to be reviewed.")
        else:
            decision_view(rid)
        batch = [r for r in db.list_runs(USER["id"], limit=-1) if row["batch_id"] and r["batch_id"] == row["batch_id"]]
        if len(batch) > 1:
            st.divider()
            st.subheader("This upload")
            for r in sorted(batch, key=lambda r: r["created_at"]):
                c1, c2, c3 = st.columns([6, 2, 2])
                c1.write(r["filename"])
                c2.markdown(badge(r["decision"], r["final_decision"]) if r["status"] == "done" else f"<small>{r['status']}</small>", unsafe_allow_html=True)
                if r["id"] != rid and r["status"] in ("done", "review") and c3.button("Open", key=f"bo{r['id']}"):
                    ss.current = r["id"]
                    st.rerun()
            filed = [f for f in folders()]
            if filed and all(r["status"] in ("done", "error") for r in batch):
                opts = {f["id"]: f["name"] for f in filed}
                c1, c2 = st.columns([3, 1])
                pick = c1.selectbox("Save all to a folder", list(opts), format_func=lambda k: opts[k], key="batchfold")
                if c2.button("Save all", use_container_width=True):
                    out = call(main.file_batch, row["batch_id"], dict(folder_id=pick), USER)
                    if out:
                        st.toast(f"{len(out['filed'])} saved in {opts[pick]}.")
    else:
        new_document()


# --------------------------------------------------------------------------------------------------- dashboard --
def dashboard_page():
    fid = ss.folder_view
    opts = {None: "All invoices"} | {f["id"]: f["name"] for f in folders()}
    if fid not in opts:
        fid = ss.folder_view = None
    c0, c1, c2 = st.columns([2, 1.2, 1.2])
    fid = c0.selectbox("Showing", list(opts), index=list(opts).index(fid), format_func=lambda k: opts[k])
    ss.folder_view = fid
    all_rows = db.list_runs(USER["id"], limit=-1)
    kind = c1.selectbox("Dated", ["All time", "Monthly", "Yearly"])
    period = ""
    dates = sorted({(r["invoice_date"] or "")[: 7 if kind == "Monthly" else 4] for r in all_rows if r["invoice_date"]}, reverse=True)
    if kind != "All time" and dates:
        period = c2.selectbox("Period", dates)
    st.header(opts[fid])
    stats = main.stats(USER, period, fid)
    xl = main.export_xlsx(USER, period, fid)
    st.download_button("Download Excel report", xl.body, file_name=xl.headers["content-disposition"].split('filename="')[1].rstrip('"'),
                       mime=main.XLSX, type="primary")
    overview, analytics_tab = st.tabs(["Overview", "Analytics"])
    with overview:
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Invoices processed", stats["total"], help=f"{stats['avg_seconds']}s average per invoice")
        m2.metric("Approved with no human touch", f"{stats['straight_through']}%", help=f"{stats['counts']['APPROVE']} approved in total")
        m3.metric("Approved for payment", money(stats["approved_value"]), help="gross, including GST")
        m4.metric("Waiting for a person", stats["counts"]["HOLD"], help=f"{money(stats['pending_value'])} on hold")
        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Outcomes**")
            n = max(stats["total"], 1)
            st.markdown(f"🟩 **{stats['counts']['APPROVE']}** approved")
            st.progress(stats["counts"]["APPROVE"] / n)
            st.markdown(f"🟧 **{stats['counts']['HOLD']}** held for review")
            st.progress(stats["counts"]["HOLD"] / n)
        with c2:
            st.markdown("**Why invoices stop**")
            top = max([r["count"] for r in stats["reasons"]] or [1])
            for r in stats["reasons"]:
                st.markdown(f"{LABEL.get(r['code'], r['code'])} · **{r['count']}**")
                st.progress(r["count"] / top)
            if not stats["reasons"]:
                st.caption("Nothing has stopped yet.")

        st.subheader("History")
        rows = [r for r in main._scope(USER, period, fid) if r["status"] == "done" and (r["final_decision"] or r["decision"]) != "REJECT"]
        search = st.text_input("Find invoice, vendor, PO or file", key="hsearch")
        if search:
            s = search.lower()
            rows = [r for r in rows if any(s in str(r.get(k) or "").lower() for k in ("filename", "vendor_name", "invoice_number", "po_number"))]
        table = [dict(Date=r["invoice_date"] or "–", File=r["filename"], Vendor=r["vendor_name"] or "–", **{"Invoice no.": r["invoice_number"] or "–"},
                      PO=r["po_number"] or "–", Total=money(r["total"]), Outcome=DECISION_WORD[r["final_decision"] or r["decision"]],
                      Why=LABEL.get(r["codes"][0], r["codes"][0]) if r["codes"] else "All checks passed", **{"Read by": r["extractor"] or ""})
                 for r in sorted(rows, key=lambda r: r["invoice_date"] or "", reverse=True)]
        if table:
            pick = st.dataframe(table, use_container_width=True, hide_index=True, on_select="rerun", selection_mode="single-row", key="hist")
            sel = pick.selection.rows if pick and pick.selection else []
            if sel:
                chosen = sorted(rows, key=lambda r: r["invoice_date"] or "", reverse=True)[sel[0]]
                with st.container(border=True):
                    decision_view(chosen["id"], key="h")
                    with st.popover("Delete this invoice for good"):
                        st.write("Its record, file and PO ledger posting are deleted. The same file uploaded again is checked as new.")
                        if st.button("Delete", type="primary", key=f"del{chosen['id']}"):
                            call(main.delete_run, chosen["id"], USER)
                            st.rerun()
        else:
            st.caption("No invoices here yet. Rejected documents stay off the Dashboard.")
        with st.popover("Reset my account"):
            st.write("Clears your invoices, PO ledger postings, approved vendors and uploads. Other accounts aren't touched.")
            if st.button("Reset", type="primary", key="reset_all"):
                call(main.reset, USER)
                ss.current, ss.watch = None, []
                st.rerun()
    with analytics_tab:
        a = main.analytics(USER, period, fid)
        if not a["total"]:
            st.caption("Nothing decided yet.")
            return
        st.markdown("**What needs you now**")
        c = st.columns(3)
        for col, k in zip(c, ("APPROVE", "HOLD", "REJECT")):
            col.metric(DECISION_WORD[k], a["outcome"][k]["count"], help=money(a["outcome"][k]["value"]))
        st.markdown("**Money on hold, by how long it has waited**")
        st.bar_chart({h["label"]: h["value"] for h in a["hold_ages"]})
        if a["spend"]:
            st.markdown("**Approved spend by invoice date**")
            st.line_chart({d: v for d, v in a["spend"]})
        st.markdown("**Vendor scorecard**")
        st.dataframe([dict(Vendor=v["name"], Invoices=v["invoices"], Stopped=v["stopped"], **{"Stop rate": f"{v['hold_rate']}%"},
                           Value=money(v["value"]), **{"Main reason": LABEL.get(v["top_reason"], v["top_reason"] or "–")}) for v in a["vendors"]],
                     use_container_width=True, hide_index=True)
        flags = [(kind, f) for kind, fs in a["flags"].items() for f in fs]
        if flags:
            st.markdown("**Fraud and control flags**")
            for kind, f in flags:
                st.markdown(f"- {LABEL.get(f['code'], f['code'])} · **{f['count']}** ({kind.replace('_', ' ')})")


# --------------------------------------------------------------------------------------------------- PO ledger --
def ledger_page():
    st.header("PO ledger")
    data = main.pos(USER)
    st.dataframe([dict(PO=p["po_number"], Vendor=p["vendor_name"], Description=p["description"], **{"PO value": money(p["net_amount"])},
                       Billed=money(p["billed"]), Remaining=money(p["remaining"]), **{"Billed %": p["pct_billed"] / 100}, Status=p["status"],
                       Invoices=", ".join(i.get("invoice_number") or i["run_id"] for i in p["invoices"]))
                  for p in data["pos"]], use_container_width=True, hide_index=True,
                 column_config={"Billed %": st.column_config.ProgressColumn("Billed", format="percent", min_value=0, max_value=1.2)})
    st.subheader("Approved vendors")
    added = {v["vendor_id"] for v in db.added_vendors(USER["id"])}
    st.dataframe([dict(ID=v["vendor_id"], Name=v["name"], GSTIN=v.get("gstin") or "–", Status=v.get("status", ""),
                       **{"Added in the app": "yes" if v["vendor_id"] in added else ""}) for v in data["vendors"]],
                 use_container_width=True, hide_index=True)
    xl = main.vendors_xlsx(USER)
    st.download_button("Download approved vendors (Excel)", xl.body, file_name=f"approved-vendors-{config.AS_OF}.xlsx", mime=main.XLSX)
    if added:
        pick = st.selectbox("Remove a vendor approved in the app", sorted(added))
        if st.button("Remove from the approved list"):
            call(main.remove_vendor, pick, USER)
            st.rerun()


# ------------------------------------------------------------------------------------------------------- rules --
RULE_TEXT = {
    "tolerance_pct": "Up to this % over the PO (cumulative, pre-tax): approved, variance recorded",
    "hold_band_pct": "Up to this % over the PO: held for a person; above it, rejected",
    "arith_abs_tol": "Arithmetic tolerance in rupees",
    "arith_rel_tol": "…or this share of the amount, whichever is larger",
    "gst_rate_tol_pp": "Invoice GST rate may differ from the PO by this many points",
    "vendor_name_strong": "Name similarity that matches a vendor without a GSTIN",
    "vendor_name_weak": "Below this similarity a name counts as unknown",
    "implied_po_min_score": "Score an implied PO match needs",
    "implied_po_min_margin": "Lead the best implied PO needs over the runner-up",
    "stale_days": "Warn if the invoice is older than this many days",
    "critical_fields": "Fields an invoice must have",
}


def rules_page():
    st.header("Rules")
    st.write("Every finding has a severity (info, warn, hold, reject). **The decision is the worst severity**, so there is no hidden score.")
    st.table({"Setting": list(CFG["rules"]), "Value": [", ".join(v) if isinstance(v, list) else str(v) for v in CFG["rules"].values()],
              "Meaning": [RULE_TEXT.get(k, "") for k in CFG["rules"]]})
    st.subheader("The stages")
    for i, s in enumerate(STAGES, 1):
        st.markdown(f"{i}. {s['title']}")
    st.subheader("Reasons an invoice stops")
    st.table({"Code": list(LABEL), "Meaning": list(LABEL.values())})


{"Process": process_page, "Dashboard": dashboard_page, "PO ledger": ledger_page, "Rules": rules_page}[ss.page]()
