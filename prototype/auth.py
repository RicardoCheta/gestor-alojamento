"""
Authentication for the host dashboard — now database-backed.

Hosts create their own account through the platform (`/signup`). Passwords are
stored only as a Werkzeug hash. The session cookie holds the host id.

Environment:
    FLASK_SECRET_KEY   signs the session cookie (set it so sessions survive restarts)
    APP_BASE_URL       public URL used in verification / reset links (prod)
"""

from __future__ import annotations

import functools
import os
import re
import secrets
import time

from flask import redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import db
import mailer

MIN_PASSWORD_LEN = 8
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# In-memory brute-force brake: lock a client for a bit after N failures.
_MAX_ATTEMPTS = 5
_LOCKOUT_SECONDS = 60
_attempts: dict[str, list[float]] = {}


def secret_key() -> str:
    key = os.environ.get("FLASK_SECRET_KEY")
    if not key:
        key = secrets.token_hex(32)
        print("WARNING: FLASK_SECRET_KEY not set — using a random key. Sessions "
              "won't survive a restart. Set it in .env for stability.")
    return key


# --------------------------------------------------------------------------- #
# Brute-force brake
# --------------------------------------------------------------------------- #

def _client_id() -> str:
    return request.headers.get("X-Forwarded-For", request.remote_addr or "?")


def locked_out() -> bool:
    now = time.time()
    hits = [t for t in _attempts.get(_client_id(), []) if now - t < _LOCKOUT_SECONDS]
    _attempts[_client_id()] = hits
    return len(hits) >= _MAX_ATTEMPTS


def _record_failure() -> None:
    _attempts.setdefault(_client_id(), []).append(time.time())


def _clear_failures() -> None:
    _attempts.pop(_client_id(), None)


# --------------------------------------------------------------------------- #
# Signup / login / session
# --------------------------------------------------------------------------- #

def validate_signup(email: str, password: str, password2: str) -> str | None:
    """Devolve uma string de erro, ou None se estiver tudo bem."""
    email = email.strip()
    if not _EMAIL_RE.match(email):
        return "Introduza um email válido."
    if len(password) < MIN_PASSWORD_LEN:
        return f"A palavra-passe tem de ter pelo menos {MIN_PASSWORD_LEN} caracteres."
    if password != password2:
        return "As duas palavras-passe não coincidem."
    if db.get_host_by_email(email):
        return "Já existe uma conta com esse email."
    return None


def _url(path: str) -> str:
    base = os.environ.get("APP_BASE_URL", "http://localhost:5051").rstrip("/")
    return f"{base}{path}"


def signup(email: str, password: str, name: str = "") -> int:
    host_id = db.create_host(email, generate_password_hash(password), name)
    token = db.create_token(host_id, "verify")
    mailer.send_verification(email.strip(), _url(f"/verify/{token}"))
    session.clear()
    session["host_id"] = host_id
    session.permanent = True
    _clear_failures()
    return host_id


def resend_verification(host) -> None:
    token = db.create_token(host["id"], "verify")
    mailer.send_verification(host["email"], _url(f"/verify/{token}"))


def verify_email(token: str) -> bool:
    row = db.get_token(token, "verify")
    if not row:
        return False
    db.mark_email_verified(row["host_id"])
    db.use_token(token)
    return True


def request_password_reset(email: str) -> None:
    host = db.get_host_by_email(email)
    if host:                       # stay quiet if the email is unknown
        token = db.create_token(host["id"], "reset")
        mailer.send_password_reset(host["email"], _url(f"/reset/{token}"))


def reset_password(token: str, new_password: str) -> str | None:
    if len(new_password) < MIN_PASSWORD_LEN:
        return f"A palavra-passe tem de ter pelo menos {MIN_PASSWORD_LEN} caracteres."
    row = db.get_token(token, "reset")
    if not row:
        return "Este link de recuperação é inválido ou já foi usado."
    db.set_password(row["host_id"], generate_password_hash(new_password))
    db.use_token(token)
    return None


LOGIN_SUSPENDED = "__suspended__"


def try_login(email: str, password: str) -> bool | str:
    host = db.get_host_by_email(email)
    # Always run a hash check so timing doesn't reveal whether the email exists.
    reference = host["password_hash"] if host else generate_password_hash("x")
    ok = check_password_hash(reference, password)
    if host and ok:
        if host["status"] == "suspended":
            return LOGIN_SUSPENDED
        session.clear()
        session["host_id"] = host["id"]
        session.permanent = True
        _clear_failures()
        return True
    _record_failure()
    return False


def logout() -> None:
    session.clear()


def current_host():
    hid = session.get("host_id")
    if not hid:
        return None
    host = db.get_host(hid)
    if host and host["status"] == "suspended":   # suspended mid-session -> kicked
        session.clear()
        return None
    return host


def is_admin() -> bool:
    host = current_host()
    return bool(host and host["role"] == "admin")


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not current_host():
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not current_host():
            return redirect(url_for("login", next=request.path))
        if not is_admin():
            return "Sem autorização", 403
        return view(*args, **kwargs)
    return wrapped
