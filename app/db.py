"""SQLite persistence: accounts, run history, the PO billing ledger, vendors approved in the app.

The ledger is the heart of split-invoice handling: every APPROVED invoice adds
its net amount against a PO, so the next invoice is judged on cumulative billing.

Every account is its own world. Anything that reads or changes one person's data takes
their user_id and filters on it in the SQL, so a missing check fails closed: another
person's run simply isn't found. Duplicate detection and PO billing are per account too.
"""
import json
import sqlite3
import threading
from datetime import datetime

from loguru import logger

from . import config

_lock = threading.RLock()
_conn = None


def conn():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        init(_conn)
    return _conn


def init(c):
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE COLLATE NOCASE, pw_hash TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS sessions(
        token_hash TEXT PRIMARY KEY, user_id INTEGER, expires_at REAL);
    CREATE TABLE IF NOT EXISTS runs(
        id TEXT PRIMARY KEY, created_at TEXT, filename TEXT, file_hash TEXT, file_path TEXT,
        status TEXT, decision TEXT, final_decision TEXT, override_note TEXT,
        vendor_name TEXT, vendor_id TEXT, invoice_number TEXT, invoice_date TEXT,
        po_number TEXT, net REAL, total REAL, extractor TEXT, duration_ms INTEGER,
        result_json TEXT, events_json TEXT, inputs_json TEXT, user_id INTEGER, stage_json TEXT, folder_id INTEGER,
        hidden INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS folders(
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, name TEXT, created_at TEXT,
        UNIQUE(user_id, name COLLATE NOCASE));
    CREATE TABLE IF NOT EXISTS ledger(
        id INTEGER PRIMARY KEY AUTOINCREMENT, po_number TEXT, run_id TEXT,
        net_amount REAL, created_at TEXT, user_id INTEGER);
    CREATE TABLE IF NOT EXISTS vendors(
        user_id INTEGER, vendor_id TEXT, name TEXT, aliases TEXT, gstin TEXT, status TEXT,
        approved_at TEXT, note TEXT, run_id TEXT, PRIMARY KEY(user_id, vendor_id));
    CREATE TABLE IF NOT EXISTS vendor_no_po(
        user_id INTEGER, vendor_id TEXT, note TEXT, set_at TEXT, run_id TEXT, PRIMARY KEY(user_id, vendor_id));
    CREATE TABLE IF NOT EXISTS vendor_aliases(
        user_id INTEGER, vendor_id TEXT, alias TEXT, added_at TEXT, run_id TEXT, PRIMARY KEY(user_id, vendor_id, alias));
    CREATE TABLE IF NOT EXISTS vendor_po_refs(
        user_id INTEGER, vendor_id TEXT, reference TEXT, added_at TEXT, run_id TEXT, PRIMARY KEY(user_id, vendor_id, reference));
    """)
    # databases made by earlier versions
    run_cols = {r[1] for r in c.execute("PRAGMA table_info(runs)")}
    if "inputs_json" not in run_cols:
        c.execute("ALTER TABLE runs ADD COLUMN inputs_json TEXT")
    if "user_id" not in run_cols:
        c.execute("ALTER TABLE runs ADD COLUMN user_id INTEGER")
    if "stage_json" not in run_cols:  # fields saved while a person reviews them
        c.execute("ALTER TABLE runs ADD COLUMN stage_json TEXT")
    if "folder_id" not in run_cols:
        c.execute("ALTER TABLE runs ADD COLUMN folder_id INTEGER")
    if "batch_id" not in run_cols:  # invoices uploaded together as a folder
        c.execute("ALTER TABLE runs ADD COLUMN batch_id TEXT")
    if "hidden" not in run_cols:  # from a version where removing only hid an invoice; purge_hidden() clears those
        c.execute("ALTER TABLE runs ADD COLUMN hidden INTEGER DEFAULT 0")
    if "user_id" not in {r[1] for r in c.execute("PRAGMA table_info(ledger)")}:
        c.execute("ALTER TABLE ledger ADD COLUMN user_id INTEGER")
    if "user_id" not in {r[1] for r in c.execute("PRAGMA table_info(vendors)")}:  # the key changes, so the table is rebuilt
        c.executescript("""
        ALTER TABLE vendors RENAME TO vendors_old;
        CREATE TABLE vendors(
            user_id INTEGER, vendor_id TEXT, name TEXT, aliases TEXT, gstin TEXT, status TEXT,
            approved_at TEXT, note TEXT, run_id TEXT, PRIMARY KEY(user_id, vendor_id));
        INSERT INTO vendors(vendor_id,name,aliases,gstin,status,approved_at,note,run_id)
            SELECT vendor_id,name,aliases,gstin,status,approved_at,note,run_id FROM vendors_old;
        DROP TABLE vendors_old;
        """)
    c.execute("CREATE INDEX IF NOT EXISTS runs_user ON runs(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS ledger_user ON ledger(user_id, po_number)")
    c.commit()


def now():
    return datetime.now().isoformat(timespec="seconds")


# ----------------------------------------------------------------- accounts --

def create_user(username, pw_hash):
    """Returns the new user's id, or None if the name is taken.

    History from before accounts existed has no owner. The first account takes it,
    so upgrading a local install keeps its data; a fresh server has nothing to take.
    """
    with _lock:
        try:
            uid = conn().execute("INSERT INTO users(username,pw_hash,created_at) VALUES(?,?,?)", (username, pw_hash, now())).lastrowid
        except sqlite3.IntegrityError:
            return None
        if conn().execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1:
            for table in ("runs", "ledger", "vendors"):
                conn().execute(f"UPDATE {table} SET user_id=? WHERE user_id IS NULL", (uid,))
        conn().commit()
    return uid


def find_user(username):
    with _lock:
        r = conn().execute("SELECT id,username,pw_hash FROM users WHERE username=?", (username,)).fetchone()
    return dict(r) if r else None


def add_session(token_hash, user_id, expires_at, now_ts):
    with _lock:
        conn().execute("DELETE FROM sessions WHERE expires_at<?", (now_ts,))
        conn().execute("INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)", (token_hash, user_id, expires_at))
        conn().commit()


def session_user(token_hash, now_ts):
    with _lock:
        r = conn().execute("SELECT u.id,u.username FROM sessions s JOIN users u ON u.id=s.user_id "
                           "WHERE s.token_hash=? AND s.expires_at>=?", (token_hash, now_ts)).fetchone()
    return dict(r) if r else None


def delete_session(token_hash):
    with _lock:
        conn().execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
        conn().commit()


# --------------------------------------------------------------------- runs --

def create_run(run_id, filename, path, user_id, file_hash=None, inputs=None, batch_id=None, status="queued"):
    with _lock:
        conn().execute("INSERT INTO runs(id,created_at,filename,file_path,file_hash,status,inputs_json,user_id,batch_id) VALUES(?,?,?,?,?,?,?,?,?)",
                       (run_id, now(), filename, str(path), file_hash, status, json.dumps(inputs or {}), user_id, batch_id))
        conn().commit()


def stuck_batches():
    """(batch id, user id) of folders with invoices still waiting but none at Review to give the approval, as after a
    restart interrupted the first one."""
    with _lock:
        rows = conn().execute("SELECT DISTINCT batch_id, user_id FROM runs w WHERE status='waiting' AND NOT EXISTS "
                              "(SELECT 1 FROM runs r WHERE r.batch_id=w.batch_id AND r.status IN ('review','queued','running'))").fetchall()
    return [(r["batch_id"], r["user_id"]) for r in rows]


def batch_waiting(batch_id, user_id):
    """A folder's invoices still held back for the approval given on its first one, in upload order."""
    with _lock:
        rows = conn().execute("SELECT * FROM runs WHERE batch_id=? AND user_id=? AND status='waiting' ORDER BY created_at, rowid",
                              (batch_id, user_id)).fetchall()
    return [dict(r) for r in rows]


def update_run(run_id, **kw):
    if not kw:
        return
    with _lock:
        cols = ",".join(f"{k}=?" for k in kw)
        conn().execute(f"UPDATE runs SET {cols} WHERE id=?", (*kw.values(), run_id))
        conn().commit()


def get_run(run_id, user_id):
    with _lock:
        r = conn().execute("SELECT * FROM runs WHERE id=? AND user_id=?", (run_id, user_id)).fetchone()
    return dict(r) if r else None


def run_owner(run_id):
    """For the background worker, which has a run id and no logged-in person."""
    with _lock:
        r = conn().execute("SELECT user_id FROM runs WHERE id=?", (run_id,)).fetchone()
    return r[0] if r else None


def run_stage(run_id):
    """What run(review=True) saved for resume(): the extracted fields and findings so far."""
    with _lock:
        r = conn().execute("SELECT stage_json FROM runs WHERE id=?", (run_id,)).fetchone()
    return r[0] if r else None


def list_runs(user_id, limit=500):
    """Newest first. limit=-1 returns every run."""
    with _lock:
        rows = conn().execute(
            "SELECT id,created_at,filename,status,decision,final_decision,override_note,vendor_name,vendor_id,"
            "invoice_number,invoice_date,po_number,net,total,extractor,duration_ms,result_json,inputs_json,folder_id,batch_id "
            "FROM runs WHERE user_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?", (user_id, limit)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        res = json.loads(d.pop("result_json") or "null") or {}
        d["inputs"] = json.loads(d.pop("inputs_json") or "{}")
        d["headline"] = res.get("headline")
        d["codes"] = [x["code"] for x in res.get("findings", []) if x["severity"] in ("hold", "reject")]
        d["flags"] = sorted({x["code"] for x in res.get("findings", [])})  # every finding, warnings included
        out.append(d)
    return out


def count_runs(user_id):
    with _lock:
        return conn().execute("SELECT COUNT(*) FROM runs WHERE user_id=?", (user_id,)).fetchone()[0]


def open_runs(user_id, waiting=False):
    """Documents the worker still has to process. waiting=True also counts a folder's invoices held for its first one's approval."""
    states = ("queued", "running", "waiting") if waiting else ("queued", "running")
    with _lock:
        return conn().execute(f"SELECT COUNT(*) FROM runs WHERE user_id=? AND status IN ({','.join('?' * len(states))})",
                              (user_id, *states)).fetchone()[0]


def delete_run(run_id, user_id):
    """Forget an invoice completely: its record and its PO ledger posting. Returns its file path, or None if not found.
    Nothing is kept, so the same file sent again is checked as new, not as a duplicate."""
    with _lock:
        r = conn().execute("SELECT file_path FROM runs WHERE id=? AND user_id=?", (run_id, user_id)).fetchone()
        if not r:
            return None
        conn().execute("DELETE FROM ledger WHERE run_id=?", (run_id,))
        conn().execute("DELETE FROM runs WHERE id=?", (run_id,))
        conn().commit()
    logger.bind(run=run_id).info("deleted for good")
    return r["file_path"]


def purge_hidden():
    """Invoices an earlier version only hid when they were removed. Removing now means deleting, so finish the job."""
    with _lock:
        rows = conn().execute("SELECT id,file_path FROM runs WHERE COALESCE(hidden,0)=1").fetchall()
        conn().executemany("DELETE FROM ledger WHERE run_id=?", [(r["id"],) for r in rows])
        conn().execute("DELETE FROM runs WHERE COALESCE(hidden,0)=1")
        conn().commit()
    return [(r["id"], r["file_path"]) for r in rows]


def fail_interrupted():
    """The queue lives in memory, so after a restart nothing will finish these. Park them for a person."""
    with _lock:
        n = conn().execute("UPDATE runs SET status='error', decision='HOLD' WHERE status IN ('queued','running')").rowcount
        conn().commit()
    return n


def prior_submissions(exclude_run_id, user_id):
    """Earlier runs that count as 'already seen' for duplicate detection.
    Rejected duplicates are excluded so they can't shadow the original."""
    with _lock:
        rows = conn().execute(
            "SELECT id,filename,file_hash,vendor_id,vendor_name,invoice_number,invoice_date,total,"
            "COALESCE(final_decision,decision) AS decision FROM runs "
            "WHERE id<>? AND user_id=? AND status='done' AND COALESCE(final_decision,decision) IN ('APPROVE','HOLD')",
            (exclude_run_id, user_id)).fetchall()
    return [dict(r) for r in rows]


def prior_by_hash(file_hash, exclude_run_id, user_id):
    with _lock:
        r = conn().execute(
            "SELECT id,filename FROM runs WHERE file_hash=? AND id<>? AND user_id=? AND status='done' "
            "AND COALESCE(final_decision,decision) IN ('APPROVE','HOLD') ORDER BY created_at LIMIT 1",
            (file_hash, exclude_run_id, user_id)).fetchone()
    return dict(r) if r else None


# ------------------------------------------------------------------- ledger --

def billed(po_number, user_id):
    with _lock:
        r = conn().execute("SELECT COALESCE(SUM(net_amount),0) FROM ledger WHERE po_number=? AND user_id=?", (po_number, user_id)).fetchone()
    return float(r[0])


def ledger_for(po_number, user_id):
    with _lock:
        rows = conn().execute(
            "SELECT l.run_id, l.net_amount, l.created_at, r.invoice_number FROM ledger l "
            "LEFT JOIN runs r ON r.id=l.run_id WHERE l.po_number=? AND l.user_id=? ORDER BY l.id", (po_number, user_id)).fetchall()
    return [dict(r) for r in rows]


def add_ledger(po_number, run_id, net):
    with _lock:
        conn().execute("DELETE FROM ledger WHERE run_id=?", (run_id,))
        conn().execute("INSERT INTO ledger(po_number,run_id,net_amount,created_at,user_id) VALUES(?,?,?,?,?)",
                       (po_number, run_id, net, now(), run_owner(run_id)))
        conn().commit()
    logger.info("ledger: {} net posted to {}", net, po_number)


def remove_ledger(run_id):
    with _lock:
        conn().execute("DELETE FROM ledger WHERE run_id=?", (run_id,))
        conn().commit()


# ------------------------------------------------------------------ vendors --

def added_vendors(user_id):
    with _lock:
        rows = conn().execute("SELECT * FROM vendors WHERE user_id=? ORDER BY approved_at, vendor_id", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def add_vendor(vendor_id, name, aliases, gstin, note, run_id, user_id):
    with _lock:
        conn().execute("INSERT INTO vendors(user_id,vendor_id,name,aliases,gstin,status,approved_at,note,run_id) VALUES(?,?,?,?,?,?,?,?,?)",
                       (user_id, vendor_id, name, aliases, gstin, "approved", now(), note, run_id))
        conn().commit()
    logger.info("vendor {} approved: {!r} GSTIN {}", vendor_id, name, gstin or "none")


def remove_vendor(vendor_id, user_id):
    with _lock:
        removed = conn().execute("DELETE FROM vendors WHERE vendor_id=? AND user_id=?", (vendor_id, user_id)).rowcount
        for table in ("vendor_no_po", "vendor_aliases", "vendor_po_refs"):
            conn().execute(f"DELETE FROM {table} WHERE vendor_id=? AND user_id=?", (vendor_id, user_id))
        conn().commit()
    if removed:
        logger.info("vendor {} removed from the approved list", vendor_id)
    return bool(removed)


def set_no_po(vendor_id, note, run_id, user_id):
    """A person has said this vendor bills without purchase orders (a restaurant, a clinic), so later bills
    from it aren't held for having no PO."""
    with _lock:
        conn().execute("INSERT OR REPLACE INTO vendor_no_po(user_id,vendor_id,note,set_at,run_id) VALUES(?,?,?,?,?)",
                       (user_id, vendor_id, note, now(), run_id))
        conn().commit()
    logger.info("vendor {} marked as not using purchase orders", vendor_id)


def no_po_vendors(user_id):
    """{vendor_id: date it was set} for vendors that don't use purchase orders."""
    with _lock:
        rows = conn().execute("SELECT vendor_id,set_at FROM vendor_no_po WHERE user_id=?", (user_id,)).fetchall()
    return {r["vendor_id"]: r["set_at"] for r in rows}


def add_alias(vendor_id, alias, run_id, user_id):
    """Another name a vendor's bills are issued under, learned when a person approved one of them."""
    with _lock:
        conn().execute("INSERT OR IGNORE INTO vendor_aliases(user_id,vendor_id,alias,added_at,run_id) VALUES(?,?,?,?,?)",
                       (user_id, vendor_id, alias, now(), run_id))
        conn().commit()
    logger.info("vendor {} also known as {!r}", vendor_id, alias)


def vendor_aliases(user_id):
    """{vendor_id: [alias, ...]} learned in the app, for master-list and app-approved vendors alike."""
    out = {}
    with _lock:
        for r in conn().execute("SELECT vendor_id,alias FROM vendor_aliases WHERE user_id=? ORDER BY added_at", (user_id,)):
            out.setdefault(r["vendor_id"], []).append(r["alias"])
    return out


def vendor_from_batch(vendor_id, batch_id, user_id):
    """Was this vendor approved while a given folder of invoices was being processed?"""
    if not batch_id:
        return False
    with _lock:
        return conn().execute("SELECT 1 FROM vendors v JOIN runs r ON r.id=v.run_id WHERE v.user_id=? AND v.vendor_id=? AND r.batch_id=?",
                              (user_id, vendor_id, batch_id)).fetchone() is not None


def accept_po_ref(vendor_id, reference, run_id, user_id):
    """A PO number this vendor cites that isn't in the PO register, accepted by a person. Stored normalised (letters and digits)."""
    with _lock:
        conn().execute("INSERT OR IGNORE INTO vendor_po_refs(user_id,vendor_id,reference,added_at,run_id) VALUES(?,?,?,?,?)",
                       (user_id, vendor_id, reference, now(), run_id))
        conn().commit()
    logger.info("vendor {}: PO reference {} accepted", vendor_id, reference)


def accepted_po_refs(user_id):
    """{(vendor_id, normalised reference): date accepted}."""
    with _lock:
        rows = conn().execute("SELECT vendor_id,reference,added_at FROM vendor_po_refs WHERE user_id=?", (user_id,)).fetchall()
    return {(r["vendor_id"], r["reference"]): r["added_at"] for r in rows}


def vendor_folder(vendor_id, exclude_run_id, user_id):
    """The folder this vendor's most recently filed bill went to, or None."""
    with _lock:
        r = conn().execute(
            "SELECT folder_id FROM runs WHERE user_id=? AND vendor_id=? AND id<>? AND folder_id IS NOT NULL "
            "AND COALESCE(final_decision,decision)<>'REJECT' ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (user_id, vendor_id, exclude_run_id)).fetchone()
    return r["folder_id"] if r else None


# ------------------------------------------------------------------ folders --

def create_folder(user_id, name):
    """Returns the new folder's id, or None if this account already has a folder by that name."""
    with _lock:
        try:
            fid = conn().execute("INSERT INTO folders(user_id,name,created_at) VALUES(?,?,?)", (user_id, name, now())).lastrowid
        except sqlite3.IntegrityError:
            return None
        conn().commit()
    return fid


def list_folders(user_id):
    with _lock:
        rows = conn().execute(
            "SELECT f.id, f.name, f.created_at, COUNT(r.id) AS count FROM folders f "
            "LEFT JOIN runs r ON r.folder_id=f.id AND r.user_id=f.user_id "
            "WHERE f.user_id=? GROUP BY f.id ORDER BY f.name COLLATE NOCASE", (user_id,)).fetchall()
    return [dict(r) for r in rows]


def get_folder(folder_id, user_id):
    with _lock:
        r = conn().execute("SELECT id,name FROM folders WHERE id=? AND user_id=?", (folder_id, user_id)).fetchone()
    return dict(r) if r else None


def count_folders(user_id):
    with _lock:
        return conn().execute("SELECT COUNT(*) FROM folders WHERE user_id=?", (user_id,)).fetchone()[0]


def reset_folder(folder_id, user_id):
    """Delete the invoices filed in one folder, and their PO ledger postings. The folder stays, empty.
    Returns their (run id, file path) pairs so the caller can remove the files."""
    with _lock:
        rows = conn().execute("SELECT id,file_path FROM runs WHERE folder_id=? AND user_id=?", (folder_id, user_id)).fetchall()
        ids = [r["id"] for r in rows]
        conn().executemany("DELETE FROM ledger WHERE run_id=?", [(i,) for i in ids])
        conn().execute("DELETE FROM runs WHERE folder_id=? AND user_id=?", (folder_id, user_id))
        conn().commit()
    logger.info("folder {} reset: {} invoice(s) removed", folder_id, len(ids))
    return [(r["id"], r["file_path"]) for r in rows]


def reset(user_id):
    """Clear one account. Returns its (run id, file path) pairs so the caller can remove the files."""
    with _lock:
        rows = conn().execute("SELECT id,file_path FROM runs WHERE user_id=?", (user_id,)).fetchall()
        conn().execute("DELETE FROM runs WHERE user_id=?", (user_id,))
        conn().execute("DELETE FROM ledger WHERE user_id=?", (user_id,))
        conn().execute("DELETE FROM vendors WHERE user_id=?", (user_id,))
        for table in ("vendor_no_po", "vendor_aliases", "vendor_po_refs", "folders"):
            conn().execute(f"DELETE FROM {table} WHERE user_id=?", (user_id,))
        conn().commit()
    return [(r["id"], r["file_path"]) for r in rows]


def close():
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None
