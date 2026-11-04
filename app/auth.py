"""Accounts and login sessions, using only the standard library.

Passwords are stored as salted scrypt hashes. A session is a random token in an
HttpOnly cookie, and only its SHA-256 is stored, so a leaked database can't be
replayed as a login. Every /api route except the ones here depends on current_user.
"""
import hashlib
import hmac
import re
import secrets
import threading
import time

from fastapi import APIRouter, Body, HTTPException, Request, Response
from loguru import logger

from . import config, db

COOKIE = "invoice_flow_session"
USERNAME_RE = re.compile(r"[A-Za-z0-9._@+-]{3,64}")
SCRYPT = dict(n=2 ** 14, r=8, p=1, maxmem=64 * 1024 * 1024)

router = APIRouter()
_attempts = {}               # throttle key -> recent attempt times. In memory: the app runs as one process
_lock = threading.Lock()


def hash_password(password):
    salt = secrets.token_bytes(16)
    return f"scrypt${salt.hex()}${hashlib.scrypt(password.encode(), salt=salt, **SCRYPT).hex()}"


def verify_password(password, stored):
    _scheme, salt, digest = stored.split("$")
    return hmac.compare_digest(hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), **SCRYPT).hex(), digest)


_NO_SUCH_USER = hash_password(secrets.token_hex(16))  # checked for unknown names, so timing doesn't reveal which names exist


def _digest(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _too_many(key, limit, window):
    with _lock:
        recent = [t for t in _attempts.get(key, []) if t > time.time() - window]
        _attempts[key] = recent
        return len(recent) >= limit


def _record(key):
    with _lock:
        _attempts.setdefault(key, []).append(time.time())


def _start_session(response, user_id):
    token = secrets.token_urlsafe(32)
    now_ts = time.time()
    db.add_session(_digest(token), user_id, now_ts + config.SESSION_DAYS * 86400, now_ts)
    response.set_cookie(COOKIE, token, max_age=config.SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=config.COOKIE_SECURE, path="/")


def current_user(request: Request):
    token = request.cookies.get(COOKIE)
    user = db.session_user(_digest(token), time.time()) if token else None
    if not user:
        raise HTTPException(401, "Please log in.")
    return user


def _ip(request):
    return request.client.host if request.client else "unknown"


@router.post("/api/signup")
def signup(request: Request, response: Response, body: dict = Body(...)):
    if not config.ALLOW_SIGNUP:
        raise HTTPException(403, "New accounts are switched off.")
    key = f"signup:{_ip(request)}"
    if _too_many(key, 10, 3600):
        raise HTTPException(429, "Too many new accounts from this address. Try again later.")
    username, password = str(body.get("username") or "").strip(), str(body.get("password") or "")
    if not USERNAME_RE.fullmatch(username):
        raise HTTPException(400, "Usernames are 3 to 64 letters, numbers or . _ @ + -")
    if not 8 <= len(password) <= 200:
        raise HTTPException(400, "Passwords need at least 8 characters.")
    _record(key)
    user_id = db.create_user(username, hash_password(password))
    if user_id is None:
        raise HTTPException(409, "That username is taken.")
    _start_session(response, user_id)
    logger.info("account created: {!r}", username)
    return dict(ok=True, username=username)


@router.post("/api/login")
def login(request: Request, response: Response, body: dict = Body(...)):
    username, password = str(body.get("username") or "").strip(), str(body.get("password") or "")[:200]
    keys = (f"name:{username.lower()}", 5), (f"ip:{_ip(request)}", 30)
    if any(_too_many(key, limit, 900) for key, limit in keys):
        raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")
    user = db.find_user(username)
    matched = verify_password(password, user["pw_hash"] if user else _NO_SUCH_USER)
    if not user or not matched:
        for key, _limit in keys:
            _record(key)
        logger.warning("failed login for {!r}", username[:64])
        raise HTTPException(401, "Wrong username or password.")
    with _lock:
        _attempts.pop(keys[0][0], None)
    _start_session(response, user["id"])
    return dict(ok=True, username=user["username"])


@router.post("/api/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(COOKIE)
    if token:
        db.delete_session(_digest(token))
    response.delete_cookie(COOKIE, path="/")
    return dict(ok=True)


@router.get("/api/me")
def me(request: Request):
    user = current_user(request)
    return dict(username=user["username"])
