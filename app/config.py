"""Central configuration: business rules, paths, extractor settings.

Everything a finance lead might want to tune lives in RULES. It is exposed
read-only in the UI (Rules tab) so the thresholds are visible, not buried.
"""
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

BASE = Path(__file__).resolve().parent.parent
load_dotenv(BASE / ".env")  # settings such as GEMINI_API_KEY; a variable already set in the environment wins
DATA_DIR = BASE / "data"
SAMPLES_DIR = BASE / "samples"
RUNTIME_DIR = Path(os.getenv("RUNTIME_DIR", BASE / "runtime"))
UPLOAD_DIR = RUNTIME_DIR / "uploads"
LOG_DIR = RUNTIME_DIR / "logs"
DB_PATH = RUNTIME_DIR / "invoices.db"
STATIC_DIR = BASE / "static"

BUYER_NAME = "Orbit Retail Pvt Ltd"

RULES = {
    # --- PO tolerance (applied to CUMULATIVE net billing vs PO net value) ---
    "tolerance_pct": 2.0,        # <= 2% over PO: approve, record variance
    "hold_band_pct": 10.0,       # 2-10% over: hold for a person; >10% over: reject
    # --- arithmetic ---
    "arith_abs_tol": 1.0,        # Rs 1 absolute (Indian invoices round to the rupee)
    "arith_rel_tol": 0.001,      # or 0.1% of the amount, whichever is larger
    "gst_rate_tol_pp": 0.6,      # invoice GST rate may differ from PO by this many points
    # --- matching ---
    "vendor_name_strong": 0.85,  # name similarity to auto-match without a GSTIN
    "vendor_name_weak": 0.60,    # below this a name is treated as unknown
    "implied_po_min_score": 0.75,
    "implied_po_min_margin": 0.15,
    # --- hygiene ---
    "stale_days": 120,           # warn if the invoice is older than this
    "critical_fields": ["vendor_name", "invoice_number", "invoice_date", "total"],
}

AS_OF = os.getenv("AS_OF_DATE") or date.today().isoformat()
EXTRACTOR = os.getenv("EXTRACTOR", "auto")          # auto | llm | rules
EXTRACT_MODEL = os.getenv("EXTRACT_MODEL", "claude-sonnet-5-5")   # with ANTHROPIC_API_KEY
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or ""
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")       # with GEMINI_API_KEY, which is used first if both are set
GEMINI_DAILY_LIMIT = int(os.getenv("GEMINI_DAILY_LIMIT", "0"))  # documents a day sent to Gemini, then the built-in reader; 0 = no limit
STAGE_DELAY = float(os.getenv("STAGE_DELAY", "0.35"))  # seconds; only so a human can watch the run view
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()     # console only; the log file always keeps DEBUG

# --- running it for other people ---
COOKIE_SECURE = os.getenv("COOKIE_SECURE", "0") == "1"  # set to 1 when served over HTTPS
ALLOW_SIGNUP = os.getenv("ALLOW_SIGNUP", "1") == "1"    # 0 once everyone who should have an account has one
SESSION_DAYS = int(os.getenv("SESSION_DAYS", "14"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "25"))              # one upload request, however many files
# 0 means no limit, the default. Set them when other people share the server: its one worker is shared, so an account
# could otherwise queue enough to make everyone else wait, and each document takes disk space
MAX_QUEUED_PER_USER = int(os.getenv("MAX_QUEUED_PER_USER", "0"))
MAX_RUNS_PER_USER = int(os.getenv("MAX_RUNS_PER_USER", "0"))
MAX_FOLDERS_PER_USER = int(os.getenv("MAX_FOLDERS_PER_USER", "100"))

for d in (RUNTIME_DIR, UPLOAD_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)
