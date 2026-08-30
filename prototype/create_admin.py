"""
Create (or reset the password of) a platform admin account.

Admins can't self-register — run this from the server:

    .venv/bin/python create_admin.py you@example.com
        -> prompts for a password

    .venv/bin/python create_admin.py you@example.com 'your-password'
        -> non-interactive

An admin logs in at the normal /login and lands on the /admin client console:
manage every host account — suspend/reactivate, change plan, toggle features.
"""

from __future__ import annotations

import getpass
import sys

from werkzeug.security import generate_password_hash

import db

MIN_LEN = 8


def main() -> None:
    if len(sys.argv) not in (2, 3):
        sys.exit("utilização: python create_admin.py <email> [palavra-passe]")
    email = sys.argv[1].strip()

    if len(sys.argv) == 3:
        password = sys.argv[2]
    else:
        password = getpass.getpass("Palavra-passe de admin: ")
        if getpass.getpass("Repita: ") != password:
            sys.exit("As palavras-passe não coincidem.")
    if len(password) < MIN_LEN:
        sys.exit(f"A palavra-passe tem de ter pelo menos {MIN_LEN} caracteres.")

    db.init_db()
    existing = db.get_host_by_email(email)
    if existing:
        db.set_password(existing["id"], generate_password_hash(password))
        with db.connect() as conn:
            conn.execute("UPDATE hosts SET role = 'admin', status = 'active', "
                         "email_verified = 1 WHERE id = ?", (existing["id"],))
        print(f"Conta {email} atualizada -> admin, palavra-passe redefinida.")
    else:
        db.create_host(email, generate_password_hash(password), name="Admin",
                       role="admin", email_verified=True)
        print(f"Conta de admin {email} criada.")
    import os
    base = os.environ.get("APP_BASE_URL", "http://localhost:5051").rstrip("/")
    print(f"Entre em {base}/login")


if __name__ == "__main__":
    main()
