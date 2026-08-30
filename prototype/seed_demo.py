"""
Dados de demonstração — um anfitrião com 2 alojamentos em Lisboa, prestadores e
reservas. Usado pelo `demo.py` (local) e, opcionalmente, em produção via
SEED_DEMO=1 para testar o circuito sem configurar SMTP.

Não é destrutivo: se `demo@example.com` já existir, não faz nada.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from werkzeug.security import generate_password_hash

import db

SEED_FILE = Path(__file__).parent / "data" / "properties.json"

DEMO_EMAIL = "demo@example.com"
DEMO_PASSWORD = "demo1234"


def seed_demo_data() -> int | None:
    """Cria o anfitrião demo + alojamentos. Devolve o host_id, ou None se já existia."""
    if db.get_host_by_email(DEMO_EMAIL):
        return None

    host_id = db.create_host(DEMO_EMAIL, generate_password_hash(DEMO_PASSWORD),
                             "Anfitrião Demo", email_verified=True)
    db.set_host_plan(host_id, "intermedio")

    raw = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    by_unit = {}
    for unit, p in raw.items():
        wifi = p.get("wifi", {})
        pid = db.create_property(host_id, unit, {
            "name": p.get("name", ""), "address": p.get("address", ""),
            "wifi_network": wifi.get("network", ""), "wifi_password": wifi.get("password", ""),
            "wifi_notes": wifi.get("notes", ""), "ac_instructions": p.get("ac_instructions", ""),
            "parking": p.get("parking", ""), "checkin": p.get("checkin", ""),
            "checkout": p.get("checkout", ""), "trash": p.get("trash", ""),
            "house_rules": p.get("house_rules", ""),
            "emergency_contact": p.get("emergency_contact", ""),
        })
        by_unit[unit] = db.get_property(pid)

    db.create_worker(host_id, "João (faz-tudo)", "+351910000001",
                     ["general", "appliance", "electrical"])
    db.create_worker(host_id, "Marta (canalizadora)", "+351910000002", ["plumbing"])
    db.create_worker(host_id, "LimpaJá", "+351910000003", ["cleaning"])

    today = date.today()
    db.create_reservation(by_unit["14B"]["id"], "Beatriz", "+351920000001",
                          today.isoformat(), (today + timedelta(days=3)).isoformat())
    db.create_reservation(by_unit["3A"]["id"], "Carlos", "+351920000002",
                          (today - timedelta(days=2)).isoformat(),
                          (today + timedelta(days=1)).isoformat())
    return host_id


def maybe_seed_demo() -> None:
    """Chamado no arranque quando SEED_DEMO está ligado."""
    import os
    if os.environ.get("SEED_DEMO", "").lower() not in ("1", "true", "yes"):
        return
    try:
        hid = seed_demo_data()
    except Exception as exc:  # noqa: BLE001
        print(f"[seed_demo] falhou: {exc}", flush=True)
        return
    if hid:
        codes = ", ".join(f"{p['unit']}={p['code']}" for p in db.list_properties(hid))
        print(f"[seed_demo] anfitrião demo criado ({DEMO_EMAIL} / {DEMO_PASSWORD}). "
              f"Códigos: {codes}", flush=True)
