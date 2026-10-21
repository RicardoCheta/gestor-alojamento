"""
Storage layer for the Gestor de Alojamento.

Runs on **SQLite** locally (zero setup) and on **Postgres** in production —
picked from the `DATABASE_URL` environment variable:

    unset / "sqlite:///..."   -> local file  data/app.db
    postgres://... / postgresql://...  -> Postgres (psycopg 3)

Multi-tenant: every property, ticket, message and guest session belongs to a
host, and all dashboard queries are scoped by host_id.

Only this module knows which backend is in use. Callers write `?` placeholders
and get dict-like rows that also accept integer indexes, on both backends.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "app.db"

_URL = os.environ.get("DATABASE_URL", "").strip()
if _URL.startswith("sqlite:///"):
    DB_PATH = Path(_URL[len("sqlite:///"):])
    _URL = ""
if _URL.startswith("postgres://"):
    _URL = "postgresql://" + _URL[len("postgres://"):]
IS_PG = _URL.startswith("postgresql://")


class _Row(dict):
    """dict row that also supports integer indexing (like sqlite3.Row)."""

    def __init__(self, cols, values):
        super().__init__(zip(cols, values))
        self.__vals = list(values)

    def __getitem__(self, key):
        if isinstance(key, int):
            return self.__vals[key]
        return dict.__getitem__(self, key)


def _pg_row_factory(cursor):
    cols = [c.name for c in cursor.description] if cursor.description else []
    return lambda values: _Row(cols, values)


class _PgConn:
    """Thin wrapper so Postgres accepts the same `?`-placeholder SQL as SQLite."""

    def __init__(self, raw):
        self._raw = raw

    def __enter__(self):
        self._raw.__enter__()
        return self

    def __exit__(self, *exc):
        return self._raw.__exit__(*exc)

    def execute(self, sql, params=()):
        return self._raw.execute(sql.replace("?", "%s"), params)


# Property columns the dashboard lets a host edit (besides unit / code).
PROPERTY_FIELDS = [
    "name", "address", "wifi_network", "wifi_password", "wifi_notes",
    "ac_instructions", "parking", "checkin", "checkout", "trash",
    "house_rules", "emergency_contact",
]

# Per-account feature flags an admin can toggle. name -> (label PT, default_on)
FEATURES = {
    "feat_ai": ("Respostas automáticas", 1),
    "feat_whatsapp": ("Canal WhatsApp", 1),
    "feat_auto_tickets": ("Criação automática de pedidos de manutenção", 1),
    "feat_dispatch": ("Envio automático de pedidos a prestadores", 1),
    "feat_proactive": ("Mensagens proativas (check-in / check-out)", 1),
    "feat_review": ("Rever respostas antes de enviar ao hóspede", 0),
}

# Maintenance categories a ticket can be classified into, and the keywords
# (PT + EN) that hint at each. A worker covers one or more of these.
TICKET_CATEGORIES = {
    "plumbing": ["chuveiro", "duche", "sanita", "autoclismo", "torneira", "lava-loiça",
                 "lavatório", "ralo", "cano", "canos", "fuga", "água", "esquentador",
                 "caldeira", "água quente", "entupido", "entupida",
                 "shower", "toilet", "tap", "faucet", "sink", "drain", "leak",
                 "leaking", "water", "pipe", "flush", "clogged", "boiler", "hot water"],
    "electrical": ["luz", "lâmpada", "tomada", "ficha", "eletricidade", "electricidade",
                   "corrente", "quadro", "disjuntor", "interruptor", "candeeiro",
                   "light", "bulb", "socket", "plug", "power", "electricity",
                   "fuse", "breaker", "switch", "lamp"],
    "appliance": ["frigorífico", "congelador", "forno", "fogão", "placa", "micro-ondas",
                  "máquina de lavar", "máquina de secar", "ar condicionado",
                  "aquecedor", "aquecimento", "televisão", "comando", "chaleira", "cafeteira",
                  "fridge", "freezer", "oven", "stove", "hob", "microwave",
                  "dishwasher", "washing machine", "dryer", "ac", "air conditioning",
                  "heater", "heating", "tv", "remote", "kettle", "coffee"],
    "cleaning": ["sujo", "suja", "nódoa", "limpar", "limpeza", "toalhas", "lençóis",
                 "roupa de cama", "cheiro", "lixo", "loiça", "aspirador",
                 "dirty", "stain", "clean", "towels", "sheets", "bedding", "smell",
                 "trash", "rubbish", "dishes", "hoover", "vacuum"],
    "general": [],  # fallback
}

CATEGORY_LABELS = {
    "plumbing": "canalização", "electrical": "eletricidade",
    "appliance": "eletrodomésticos", "cleaning": "limpeza", "general": "geral",
}

# Ticket priority + status keep their stored English values; these are for display.
PRIORITY_LABELS = {"High": "Alta", "Medium": "Média", "Low": "Baixa"}
STATUS_LABELS = {"open": "aberto", "in progress": "em curso", "done": "concluído"}

# Plans: property limit (None = unlimited) and price €/month. No real billing yet.
# Todas as contas são pagas — não há plano gratuito.
PLANS = {
    "inicial": {"label": "Inicial", "max_properties": 1, "price": 10},
    "intermedio": {"label": "Intermédio", "max_properties": 5, "price": 25},
    "pro": {"label": "Pro", "max_properties": None, "price": 99},
}
DEFAULT_PLAN = "inicial"

# Chaves antigas -> chave atual (para bases de dados já existentes).
_PLAN_ALIASES = {"free": "inicial", "individual": "inicial", "starter": "intermedio"}

# Default automatic-message templates for a new property. Placeholders in {chaves}
# are filled from the reservation + property (see scheduler.render_template).
DEFAULT_TPL_PRECHECKIN = (
    "Olá {hospede}! É hoje o check-in 🎉\n"
    "Morada: {morada}\n"
    "Entrada: {entrada}\n"
    "WiFi: rede \"{wifi_rede}\", palavra-passe {wifi_senha}\n"
    "Qualquer questão, é só responder a esta mensagem. Boa viagem!"
)
DEFAULT_TPL_CHECKOUT = (
    "Olá {hospede}! Um lembrete: o check-out é amanhã.\n"
    "Saída: {saida}\n"
    "Obrigado pela estadia — foi um prazer recebê-lo(a)!"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts (
    id            {PK},
    email         TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL DEFAULT 'host',      -- 'host' | 'admin'
    status        TEXT NOT NULL DEFAULT 'active',    -- 'active' | 'suspended'
    plan          TEXT NOT NULL DEFAULT 'inicial',
    email_verified    INTEGER NOT NULL DEFAULT 0,
    feat_ai           INTEGER NOT NULL DEFAULT 1,
    feat_whatsapp     INTEGER NOT NULL DEFAULT 1,
    feat_auto_tickets INTEGER NOT NULL DEFAULT 1,
    feat_dispatch     INTEGER NOT NULL DEFAULT 1,
    feat_proactive    INTEGER NOT NULL DEFAULT 1,
    feat_review       INTEGER NOT NULL DEFAULT 0,
    requested_plan    TEXT NOT NULL DEFAULT '',
    stripe_customer_id     TEXT NOT NULL DEFAULT '',
    stripe_subscription_id TEXT NOT NULL DEFAULT '',
    subscription_status    TEXT NOT NULL DEFAULT '',   -- '' | active | trialing | past_due | canceled
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS auth_tokens (
    token      TEXT PRIMARY KEY,
    host_id    INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,                        -- 'verify' | 'reset'
    created_at TEXT NOT NULL,
    used_at    TEXT
);

CREATE TABLE IF NOT EXISTS properties (
    id               {PK},
    host_id          INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    code             TEXT NOT NULL UNIQUE,
    unit             TEXT NOT NULL,
    name             TEXT NOT NULL DEFAULT '',
    address          TEXT NOT NULL DEFAULT '',
    wifi_network     TEXT NOT NULL DEFAULT '',
    wifi_password    TEXT NOT NULL DEFAULT '',
    wifi_notes       TEXT NOT NULL DEFAULT '',
    ac_instructions  TEXT NOT NULL DEFAULT '',
    parking          TEXT NOT NULL DEFAULT '',
    checkin          TEXT NOT NULL DEFAULT '',
    checkout         TEXT NOT NULL DEFAULT '',
    trash            TEXT NOT NULL DEFAULT '',
    house_rules      TEXT NOT NULL DEFAULT '',
    emergency_contact TEXT NOT NULL DEFAULT '',
    tpl_precheckin   TEXT NOT NULL DEFAULT '',
    tpl_checkout     TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    UNIQUE (host_id, unit)
);

CREATE TABLE IF NOT EXISTS reservations (
    id            {PK},
    property_id   INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    guest_name    TEXT NOT NULL DEFAULT '',
    guest_phone   TEXT NOT NULL DEFAULT '',        -- E.164
    check_in      TEXT NOT NULL,                   -- YYYY-MM-DD
    check_out     TEXT NOT NULL,                   -- YYYY-MM-DD
    precheckin_sent_at TEXT,
    checkout_sent_at   TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workers (
    id         {PK},
    host_id    INTEGER NOT NULL REFERENCES hosts(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    phone      TEXT NOT NULL,                -- E.164, e.g. +351912000111
    trades     TEXT NOT NULL DEFAULT 'general',  -- comma-separated categories
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tickets (
    id          {PK},
    property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    issue       TEXT NOT NULL,
    priority    TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT 'general',
    status      TEXT NOT NULL DEFAULT 'open',
    guest       TEXT NOT NULL DEFAULT '',
    raw_message TEXT NOT NULL DEFAULT '',
    assigned_worker_id INTEGER REFERENCES workers(id) ON DELETE SET NULL,
    assigned_at TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id          {PK},
    property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    guest       TEXT NOT NULL DEFAULT '',
    body        TEXT NOT NULL,
    reply       TEXT NOT NULL,
    kind        TEXT NOT NULL,
    topic       TEXT,
    ticket_id   INTEGER REFERENCES tickets(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS guest_sessions (
    phone       TEXT PRIMARY KEY,
    property_id INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS review_items (
    id            {PK},
    property_id   INTEGER NOT NULL REFERENCES properties(id) ON DELETE CASCADE,
    guest         TEXT NOT NULL DEFAULT '',
    guest_phone   TEXT NOT NULL DEFAULT '',        -- 'whatsapp:+351...'
    guest_message TEXT NOT NULL,
    draft_reply   TEXT NOT NULL,
    proposed_kind TEXT NOT NULL DEFAULT 'answer',  -- 'answer' | 'ticket' | 'forwarded'
    topic         TEXT,
    issue         TEXT NOT NULL DEFAULT '',
    priority      TEXT NOT NULL DEFAULT 'Medium',
    category      TEXT NOT NULL DEFAULT 'general',
    status        TEXT NOT NULL DEFAULT 'pending', -- 'pending' | 'approved' | 'discarded'
    created_at    TEXT NOT NULL,
    resolved_at   TEXT
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# Extra indexes (portable). The email one replaces `UNIQUE COLLATE NOCASE`.
_INDEXES = [
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_hosts_email ON hosts (LOWER(email))",
    "CREATE INDEX IF NOT EXISTS ix_properties_host ON properties (host_id)",
    "CREATE INDEX IF NOT EXISTS ix_tickets_property ON tickets (property_id)",
    "CREATE INDEX IF NOT EXISTS ix_messages_property ON messages (property_id)",
    "CREATE INDEX IF NOT EXISTS ix_workers_host ON workers (host_id)",
    "CREATE INDEX IF NOT EXISTS ix_reservations_property ON reservations (property_id)",
    "CREATE INDEX IF NOT EXISTS ix_review_property ON review_items (property_id)",
]


def _schema_statements() -> list[str]:
    pk = ("INTEGER GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY" if IS_PG
          else "INTEGER PRIMARY KEY AUTOINCREMENT")
    ddl = SCHEMA.format(PK=pk)
    return [s.strip() for s in ddl.split(";") if s.strip()] + _INDEXES


def connect():
    if IS_PG:
        import psycopg
        return _PgConn(psycopg.connect(_URL, row_factory=_pg_row_factory))
    DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after the first release — applied to existing DBs on init.
_MIGRATIONS = {
    "hosts": {
        "role": "TEXT NOT NULL DEFAULT 'host'",
        "status": "TEXT NOT NULL DEFAULT 'active'",
        "plan": "TEXT NOT NULL DEFAULT 'inicial'",
        "email_verified": "INTEGER NOT NULL DEFAULT 0",
        "feat_ai": "INTEGER NOT NULL DEFAULT 1",
        "feat_whatsapp": "INTEGER NOT NULL DEFAULT 1",
        "feat_auto_tickets": "INTEGER NOT NULL DEFAULT 1",
        "feat_dispatch": "INTEGER NOT NULL DEFAULT 1",
        "feat_proactive": "INTEGER NOT NULL DEFAULT 1",
        "feat_review": "INTEGER NOT NULL DEFAULT 0",
        "requested_plan": "TEXT NOT NULL DEFAULT ''",
        "stripe_customer_id": "TEXT NOT NULL DEFAULT ''",
        "stripe_subscription_id": "TEXT NOT NULL DEFAULT ''",
        "subscription_status": "TEXT NOT NULL DEFAULT ''",
    },
    "tickets": {
        "category": "TEXT NOT NULL DEFAULT 'general'",
        "assigned_worker_id": "INTEGER",
        "assigned_at": "TEXT",
    },
    "properties": {
        "tpl_precheckin": "TEXT NOT NULL DEFAULT ''",
        "tpl_checkout": "TEXT NOT NULL DEFAULT ''",
    },
}


def _existing_columns(conn, table: str) -> set[str]:
    if IS_PG:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?",
            (table,),
        )
    else:
        rows = conn.execute(f"PRAGMA table_info({table})")
    return {r["column_name"] if IS_PG else r["name"] for r in rows}


def init_db() -> None:
    with connect() as conn:
        # Serialise concurrent boots (e.g. web + worker processes on Render) so
        # schema DDL and the idempotent data migrations don't race / deadlock.
        if IS_PG:
            conn.execute("SELECT pg_advisory_xact_lock(4242424242)")
        for stmt in _schema_statements():
            conn.execute(stmt)
        for table, cols in _MIGRATIONS.items():
            existing = _existing_columns(conn, table)
            for col, decl in cols.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        # Renomear planos antigos e apanhar quaisquer valores desconhecidos.
        for old, new in _PLAN_ALIASES.items():
            conn.execute("UPDATE hosts SET plan = ? WHERE plan = ?", (new, old))
        placeholders = ",".join("?" for _ in PLANS)
        conn.execute(
            f"UPDATE hosts SET plan = ? WHERE plan NOT IN ({placeholders})",
            (DEFAULT_PLAN, *PLANS.keys()),
        )
        conn.execute(
            "UPDATE hosts SET requested_plan = '' "
            f"WHERE requested_plan != '' AND requested_plan NOT IN ({placeholders})",
            tuple(PLANS.keys()),
        )


def reset_db() -> None:
    """Drop everything and recreate — used by demo.py (local dev)."""
    if IS_PG:
        if os.environ.get("ALLOW_DB_RESET") != "1":
            raise RuntimeError(
                "reset_db() em Postgres apaga tudo — defina ALLOW_DB_RESET=1 para confirmar.")
        with connect() as conn:
            conn.execute("DROP SCHEMA public CASCADE")
            conn.execute("CREATE SCHEMA public")
    elif DB_PATH.exists():
        DB_PATH.unlink()
    init_db()


def _insert(conn, sql: str, params=()) -> int:
    """INSERT returning the new row's id — portable across SQLite 3.35+ and Postgres."""
    row = conn.execute(sql.rstrip().rstrip(";") + " RETURNING id", params).fetchone()
    return row["id"]


# --------------------------------------------------------------------------- #
# Hosts
# --------------------------------------------------------------------------- #

def create_host(email: str, password_hash: str, name: str = "",
                role: str = "host", email_verified: bool = False) -> int:
    with connect() as conn:
        return _insert(
            conn,
            "INSERT INTO hosts (email, password_hash, name, role, email_verified, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (email.strip(), password_hash, name.strip(), role,
             1 if email_verified else 0, now()),
        )


def get_host_by_email(email: str):
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM hosts WHERE LOWER(email) = LOWER(?)", (email.strip(),)
        ).fetchone()


def get_host(host_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM hosts WHERE id = ?", (host_id,)).fetchone()


def count_hosts() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM hosts").fetchone()[0]


def set_password(host_id: int, password_hash: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE hosts SET password_hash = ? WHERE id = ?",
                     (password_hash, host_id))


def mark_email_verified(host_id: int) -> None:
    with connect() as conn:
        conn.execute("UPDATE hosts SET email_verified = 1 WHERE id = ?", (host_id,))


# --------------------------------------------------------------------------- #
# Admin: managing client accounts
# --------------------------------------------------------------------------- #

def list_hosts_with_stats() -> list[dict]:
    """All non-admin accounts plus property / ticket / message counts."""
    with connect() as conn:
        rows = conn.execute("""
            SELECT h.*,
              (SELECT COUNT(*) FROM properties p WHERE p.host_id = h.id) AS n_properties,
              (SELECT COUNT(*) FROM tickets t JOIN properties p ON p.id = t.property_id
                 WHERE p.host_id = h.id AND t.status != 'done')          AS n_open_tickets,
              (SELECT COUNT(*) FROM messages m JOIN properties p ON p.id = m.property_id
                 WHERE p.host_id = h.id)                                 AS n_messages
            FROM hosts h
            WHERE h.role = 'host'
            ORDER BY h.created_at DESC
        """).fetchall()
        return [dict(r) for r in rows]


def set_host_status(host_id: int, status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE hosts SET status = ? WHERE id = ? AND role = 'host'",
                     (status, host_id))


def set_host_plan(host_id: int, plan: str) -> None:
    if plan not in PLANS:
        return
    with connect() as conn:
        conn.execute("UPDATE hosts SET plan = ?, requested_plan = '' "
                     "WHERE id = ? AND role = 'host'", (plan, host_id))


def request_plan(host_id: int, plan: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE hosts SET requested_plan = ? WHERE id = ? AND role = 'host'",
                     (plan if plan in PLANS else "", host_id))


# --- Stripe ------------------------------------------------------------------

def set_stripe_customer(host_id: int, customer_id: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE hosts SET stripe_customer_id = ? WHERE id = ?",
                     (customer_id, host_id))


def get_host_by_stripe_customer(customer_id: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute("SELECT * FROM hosts WHERE stripe_customer_id = ?",
                            (customer_id,)).fetchone()


def set_subscription(host_id: int, subscription_id: str, status: str,
                     plan: str | None) -> None:
    with connect() as conn:
        if plan in PLANS:
            conn.execute(
                "UPDATE hosts SET stripe_subscription_id = ?, subscription_status = ?, "
                "plan = ?, requested_plan = '' WHERE id = ?",
                (subscription_id, status, plan, host_id))
        else:
            conn.execute(
                "UPDATE hosts SET stripe_subscription_id = ?, subscription_status = ? "
                "WHERE id = ?", (subscription_id, status, host_id))


def host_analytics(host_id: int) -> dict:
    """Simple counts for the admin client view and the host's own billing page."""
    with connect() as conn:
        def one(sql, *a):
            return conn.execute(sql, a).fetchone()[0]
        total_msgs = one("SELECT COUNT(*) FROM messages m JOIN properties p ON p.id=m.property_id "
                         "WHERE p.host_id=?", host_id)
        inbound = one("SELECT COUNT(*) FROM messages m JOIN properties p ON p.id=m.property_id "
                      "WHERE p.host_id=? AND m.kind IN ('answer','ticket','forwarded')", host_id)
        answered = one("SELECT COUNT(*) FROM messages m JOIN properties p ON p.id=m.property_id "
                       "WHERE p.host_id=? AND m.kind='answer'", host_id)
        last7 = one("SELECT COUNT(*) FROM messages m JOIN properties p ON p.id=m.property_id "
                    "WHERE p.host_id=? AND m.created_at >= ?",
                    host_id, (date.today() - timedelta(days=7)).isoformat())
        tickets = one("SELECT COUNT(*) FROM tickets t JOIN properties p ON p.id=t.property_id "
                      "WHERE p.host_id=?", host_id)
        by_cat = conn.execute(
            "SELECT t.category AS c, COUNT(*) AS n FROM tickets t "
            "JOIN properties p ON p.id=t.property_id WHERE p.host_id=? "
            "GROUP BY t.category ORDER BY n DESC", (host_id,)).fetchall()
        resa = one("SELECT COUNT(*) FROM reservations r JOIN properties p ON p.id=r.property_id "
                   "WHERE p.host_id=?", host_id)
    return {
        "messages": total_msgs, "messages_7d": last7,
        "auto_rate": round(100 * answered / inbound) if inbound else 0,
        "tickets": tickets, "tickets_by_category": [(r["c"], r["n"]) for r in by_cat],
        "reservations": resa,
    }


def set_host_feature(host_id: int, feature: str, on: bool) -> None:
    if feature not in FEATURES:
        return
    with connect() as conn:
        conn.execute(f"UPDATE hosts SET {feature} = ? WHERE id = ? AND role = 'host'",
                     (1 if on else 0, host_id))


def property_limit_reached(host) -> bool:
    limit = PLANS.get(host["plan"], PLANS[DEFAULT_PLAN])["max_properties"]
    if limit is None:
        return False
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) FROM properties WHERE host_id = ?",
                         (host["id"],)).fetchone()[0]
    return n >= limit


def recent_activity(limit: int = 25) -> list[sqlite3.Row]:
    """Latest guest messages across every client — for the admin overview."""
    with connect() as conn:
        return conn.execute(
            "SELECT m.created_at, m.guest, m.body, m.kind, p.unit AS unit, "
            "h.email AS host_email "
            "FROM messages m JOIN properties p ON p.id = m.property_id "
            "JOIN hosts h ON h.id = p.host_id "
            "ORDER BY m.created_at DESC LIMIT ?", (limit,)
        ).fetchall()


# --------------------------------------------------------------------------- #
# Auth tokens (email verification + password reset)
# --------------------------------------------------------------------------- #

def create_token(host_id: int, kind: str) -> str:
    token = secrets.token_urlsafe(24)
    with connect() as conn:
        conn.execute(
            "INSERT INTO auth_tokens (token, host_id, kind, created_at) VALUES (?, ?, ?, ?)",
            (token, host_id, kind, now()),
        )
    return token


def get_token(token: str, kind: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM auth_tokens WHERE token = ? AND kind = ? AND used_at IS NULL",
            (token, kind),
        ).fetchone()


def use_token(token: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE auth_tokens SET used_at = ? WHERE token = ?", (now(), token))


# --------------------------------------------------------------------------- #
# Properties
# --------------------------------------------------------------------------- #

def _unique_code(conn) -> str:
    while True:
        code = secrets.token_hex(3)  # 6 hex chars, e.g. "a1b2c3"
        if not conn.execute("SELECT 1 FROM properties WHERE code = ?", (code,)).fetchone():
            return code


def create_property(host_id: int, unit: str, fields: dict | None = None) -> int:
    fields = fields or {}
    cols = ["host_id", "code", "unit", "created_at",
            "tpl_precheckin", "tpl_checkout"]
    vals = [host_id, None, unit.strip(), now(),
            fields.get("tpl_precheckin") or DEFAULT_TPL_PRECHECKIN,
            fields.get("tpl_checkout") or DEFAULT_TPL_CHECKOUT]
    for f in PROPERTY_FIELDS:
        cols.append(f)
        vals.append((fields.get(f) or "").strip())
    with connect() as conn:
        vals[1] = _unique_code(conn)
        placeholders = ", ".join("?" for _ in cols)
        return _insert(
            conn, f"INSERT INTO properties ({', '.join(cols)}) VALUES ({placeholders})", vals
        )


def list_properties(host_id: int) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM properties WHERE host_id = ? ORDER BY unit", (host_id,)
        ).fetchall()


def get_property(property_id: int, host_id: int | None = None) -> sqlite3.Row | None:
    """Fetch a property. Pass host_id to enforce ownership (returns None if not theirs)."""
    with connect() as conn:
        if host_id is None:
            return conn.execute(
                "SELECT * FROM properties WHERE id = ?", (property_id,)
            ).fetchone()
        return conn.execute(
            "SELECT * FROM properties WHERE id = ? AND host_id = ?",
            (property_id, host_id),
        ).fetchone()


def get_property_by_code(code: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT * FROM properties WHERE code = ?", (code.strip().lower(),)
        ).fetchone()


def update_property(property_id: int, host_id: int, fields: dict) -> bool:
    sets, vals = [], []
    for f in [*PROPERTY_FIELDS, "unit", "tpl_precheckin", "tpl_checkout"]:
        if f in fields:
            sets.append(f"{f} = ?")
            vals.append((fields.get(f) or "").strip())
    if not sets:
        return False
    vals += [property_id, host_id]
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE properties SET {', '.join(sets)} WHERE id = ? AND host_id = ?", vals
        )
        return cur.rowcount > 0


def delete_property(property_id: int, host_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM properties WHERE id = ? AND host_id = ?", (property_id, host_id)
        )
        return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Reservations
# --------------------------------------------------------------------------- #

def create_reservation(property_id: int, guest_name: str, guest_phone: str,
                       check_in: str, check_out: str) -> int:
    with connect() as conn:
        return _insert(
            conn,
            "INSERT INTO reservations (property_id, guest_name, guest_phone, "
            "check_in, check_out, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (property_id, guest_name.strip(), guest_phone.strip(),
             check_in, check_out, now()),
        )


_RES_SELECT = (
    "SELECT r.*, p.unit AS unit, p.host_id AS host_id FROM reservations r "
    "JOIN properties p ON p.id = r.property_id "
)


def list_reservations(host_id: int) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            _RES_SELECT + "WHERE p.host_id = ? ORDER BY r.check_in DESC", (host_id,)
        ).fetchall()


def get_reservation(res_id: int, host_id: int | None = None) -> sqlite3.Row | None:
    with connect() as conn:
        row = conn.execute(_RES_SELECT + "WHERE r.id = ?", (res_id,)).fetchone()
    if row and host_id is not None and row["host_id"] != host_id:
        return None
    return row


def update_reservation(res_id: int, host_id: int, guest_name: str, guest_phone: str,
                       check_in: str, check_out: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE reservations SET guest_name = ?, guest_phone = ?, check_in = ?, "
            "check_out = ? WHERE id = ? AND property_id IN "
            "(SELECT id FROM properties WHERE host_id = ?)",
            (guest_name.strip(), guest_phone.strip(), check_in, check_out, res_id, host_id),
        )
        return cur.rowcount > 0


def delete_reservation(res_id: int, host_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM reservations WHERE id = ? AND property_id IN "
            "(SELECT id FROM properties WHERE host_id = ?)", (res_id, host_id),
        )
        return cur.rowcount > 0


def reservations_due(kind: str, on_date: str) -> list[sqlite3.Row]:
    """kind 'precheckin' -> check_in == on_date and not yet sent.
       kind 'checkout'   -> check_out == on_date and not yet sent."""
    col = "check_in" if kind == "precheckin" else "check_out"
    sent = "precheckin_sent_at" if kind == "precheckin" else "checkout_sent_at"
    with connect() as conn:
        return conn.execute(
            _RES_SELECT + f"JOIN hosts h ON h.id = p.host_id "
            f"WHERE r.{col} = ? AND r.{sent} IS NULL "
            f"AND h.status = 'active' AND h.feat_proactive = 1 AND h.feat_whatsapp = 1",
            (on_date,),
        ).fetchall()


def mark_reservation_sent(res_id: int, kind: str) -> None:
    col = "precheckin_sent_at" if kind == "precheckin" else "checkout_sent_at"
    with connect() as conn:
        conn.execute(f"UPDATE reservations SET {col} = ? WHERE id = ?", (now(), res_id))


# --------------------------------------------------------------------------- #
# Tickets
# --------------------------------------------------------------------------- #

def create_ticket(property_id: int, issue: str, priority: str,
                  raw_message: str, guest: str,
                  category: str = "general") -> sqlite3.Row:
    with connect() as conn:
        return conn.execute(
            "INSERT INTO tickets (property_id, issue, priority, category, raw_message, "
            "guest, created_at) VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING *",
            (property_id, issue, priority, category, raw_message, guest, now()),
        ).fetchone()


_TICKET_SELECT = (
    "SELECT t.*, p.unit AS unit, p.host_id AS host_id, "
    "w.name AS worker_name, w.phone AS worker_phone "
    "FROM tickets t JOIN properties p ON p.id = t.property_id "
    "LEFT JOIN workers w ON w.id = t.assigned_worker_id "
)


def list_tickets(host_id: int) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            _TICKET_SELECT + "WHERE p.host_id = ? ORDER BY t.created_at DESC", (host_id,)
        ).fetchall()


def get_ticket(ticket_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(_TICKET_SELECT + "WHERE t.id = ?", (ticket_id,)).fetchone()


def set_ticket_status(ticket_id: int, host_id: int, status: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE tickets SET status = ? WHERE id = ? AND property_id IN "
            "(SELECT id FROM properties WHERE host_id = ?)",
            (status, ticket_id, host_id),
        )
        return cur.rowcount > 0


def set_ticket_status_direct(ticket_id: int, status: str) -> None:
    """Unscoped status change — used by the worker-reply flow, which has already
    been authorised by matching the worker's phone number."""
    with connect() as conn:
        conn.execute("UPDATE tickets SET status = ? WHERE id = ?", (status, ticket_id))


def assign_ticket(ticket_id: int, worker_id: int | None, host_id: int | None = None) -> bool:
    guard = " AND property_id IN (SELECT id FROM properties WHERE host_id = ?)" if host_id else ""
    params = [worker_id, now() if worker_id else None, ticket_id]
    if host_id:
        params.append(host_id)
    with connect() as conn:
        cur = conn.execute(
            f"UPDATE tickets SET assigned_worker_id = ?, assigned_at = ? WHERE id = ?{guard}",
            params,
        )
        return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Workers (cleaners / handymen a host dispatches tickets to)
# --------------------------------------------------------------------------- #

def create_worker(host_id: int, name: str, phone: str, trades: list[str]) -> int:
    with connect() as conn:
        return _insert(
            conn,
            "INSERT INTO workers (host_id, name, phone, trades, created_at) VALUES (?, ?, ?, ?, ?)",
            (host_id, name.strip(), phone.strip(), ",".join(trades) or "general", now()),
        )


def list_workers(host_id: int, active_only: bool = False) -> list[sqlite3.Row]:
    q = "SELECT * FROM workers WHERE host_id = ?"
    if active_only:
        q += " AND active = 1"
    with connect() as conn:
        return conn.execute(q + " ORDER BY name", (host_id,)).fetchall()


def get_worker(worker_id: int, host_id: int | None = None) -> sqlite3.Row | None:
    q = "SELECT * FROM workers WHERE id = ?"
    params = [worker_id]
    if host_id is not None:
        q += " AND host_id = ?"
        params.append(host_id)
    with connect() as conn:
        return conn.execute(q, params).fetchone()


def get_worker_by_phone(phone: str) -> sqlite3.Row | None:
    digits = "".join(c for c in phone if c.isdigit())[-9:]  # match on last 9 digits
    with connect() as conn:
        for w in conn.execute("SELECT * FROM workers WHERE active = 1"):
            if "".join(c for c in w["phone"] if c.isdigit())[-9:] == digits:
                return w
    return None


def update_worker(worker_id: int, host_id: int, name: str, phone: str,
                  trades: list[str], active: bool) -> bool:
    with connect() as conn:
        cur = conn.execute(
            "UPDATE workers SET name = ?, phone = ?, trades = ?, active = ? "
            "WHERE id = ? AND host_id = ?",
            (name.strip(), phone.strip(), ",".join(trades) or "general",
             1 if active else 0, worker_id, host_id),
        )
        return cur.rowcount > 0


def delete_worker(worker_id: int, host_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("DELETE FROM workers WHERE id = ? AND host_id = ?",
                           (worker_id, host_id))
        return cur.rowcount > 0


def pick_worker_for(host_id: int, category: str) -> sqlite3.Row | None:
    """First active worker whose trades cover the category (or 'general')."""
    workers = list_workers(host_id, active_only=True)
    for w in workers:
        trades = [t.strip() for t in w["trades"].split(",")]
        if category in trades:
            return w
    for w in workers:
        if "general" in [t.strip() for t in w["trades"].split(",")]:
            return w
    return None


def latest_open_ticket_for_worker(worker_id: int) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            _TICKET_SELECT + "WHERE t.assigned_worker_id = ? AND t.status != 'done' "
            "ORDER BY t.assigned_at DESC, t.id DESC LIMIT 1",
            (worker_id,),
        ).fetchone()


# --------------------------------------------------------------------------- #
# Messages
# --------------------------------------------------------------------------- #

def log_message(property_id: int, guest: str, body: str, reply: str,
                kind: str, topic: str | None, ticket_id: int | None) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO messages (property_id, guest, body, reply, kind, topic, ticket_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (property_id, guest, body, reply, kind, topic, ticket_id, now()),
        )


def list_messages(host_id: int) -> list[sqlite3.Row]:
    with connect() as conn:
        return conn.execute(
            "SELECT m.*, p.unit AS unit FROM messages m "
            "JOIN properties p ON p.id = m.property_id "
            "WHERE p.host_id = ? ORDER BY m.created_at",
            (host_id,),
        ).fetchall()


# --------------------------------------------------------------------------- #
# Guest sessions (phone number -> property)
# --------------------------------------------------------------------------- #

def get_guest_property(phone: str) -> sqlite3.Row | None:
    with connect() as conn:
        return conn.execute(
            "SELECT p.* FROM guest_sessions g JOIN properties p ON p.id = g.property_id "
            "WHERE g.phone = ?",
            (phone,),
        ).fetchone()


def set_guest_property(phone: str, property_id: int) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO guest_sessions (phone, property_id, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(phone) DO UPDATE SET property_id = excluded.property_id, "
            "updated_at = excluded.updated_at",
            (phone, property_id, now()),
        )


# --------------------------------------------------------------------------- #
# Review queue (draft replies awaiting host approval — feat_review)
# --------------------------------------------------------------------------- #

def create_review_item(property_id: int, guest: str, guest_phone: str,
                       guest_message: str, plan: dict) -> int:
    with connect() as conn:
        return _insert(
            conn,
            "INSERT INTO review_items (property_id, guest, guest_phone, guest_message, "
            "draft_reply, proposed_kind, topic, issue, priority, category, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (property_id, guest, guest_phone, guest_message,
             plan["reply"], plan["kind"], plan.get("topic"),
             plan.get("issue", ""), plan.get("priority", "Medium"),
             plan.get("category", "general"), now()),
        )


_REVIEW_SELECT = (
    "SELECT r.*, p.unit AS unit, p.host_id AS host_id FROM review_items r "
    "JOIN properties p ON p.id = r.property_id "
)


def list_review_items(host_id: int, pending_only: bool = True) -> list[sqlite3.Row]:
    q = _REVIEW_SELECT + "WHERE p.host_id = ?"
    if pending_only:
        q += " AND r.status = 'pending'"
    with connect() as conn:
        return conn.execute(q + " ORDER BY r.created_at DESC", (host_id,)).fetchall()


def get_review_item(item_id: int, host_id: int | None = None) -> sqlite3.Row | None:
    with connect() as conn:
        row = conn.execute(_REVIEW_SELECT + "WHERE r.id = ?", (item_id,)).fetchone()
    if row and host_id is not None and row["host_id"] != host_id:
        return None
    return row


def resolve_review_item(item_id: int, status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE review_items SET status = ?, resolved_at = ? WHERE id = ?",
                     (status, now(), item_id))


def count_pending_reviews(host_id: int) -> int:
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM review_items r JOIN properties p ON p.id = r.property_id "
            "WHERE p.host_id = ? AND r.status = 'pending'", (host_id,)
        ).fetchone()["n"]


if __name__ == "__main__":
    init_db()
    print(f"Base de dados inicializada ({'Postgres' if IS_PG else DB_PATH}).")
