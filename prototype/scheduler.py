"""
Mensagens proativas: envia instruções de check-in no dia da chegada e um
lembrete de check-out na véspera da saída, por WhatsApp, a partir das reservas.

Correr manualmente (ou por cron, ex.: */30 * * * *):

    .venv/bin/python scheduler.py

O dashboard.py também chama `run_due()` de 10 em 10 minutos numa thread de fundo.
"""

from __future__ import annotations

import re
import sys
from datetime import date, timedelta

import db
from notifications import send_whatsapp


def _time_from(text: str, default: str) -> str:
    m = re.search(r"(\d{1,2})[:h](\d{2})", text) or re.search(r"\b(\d{1,2})\s*h\b", text)
    if not m:
        return default
    return f"{m.group(1)}:{m.group(2)}" if m.lastindex == 2 else f"{m.group(1)}h"


def render_template(tpl: str, res, prop) -> str:
    ctx = {
        "hospede": res["guest_name"] or "Olá",
        "unidade": prop["unit"],
        "morada": prop["address"] or "(a confirmar)",
        "codigo": prop["code"],
        "entrada": prop["checkin"] or "",
        "saida": prop["checkout"] or "",
        "checkin_hora": _time_from(prop["checkin"] or "", "combinada"),
        "checkout_hora": _time_from(prop["checkout"] or "", "combinada"),
        "wifi_rede": prop["wifi_network"] or "(a confirmar)",
        "wifi_senha": prop["wifi_password"] or "(a confirmar)",
    }

    class _Safe(dict):
        def __missing__(self, k):  # unknown {chave} -> fica vazio
            return ""

    out = tpl.format_map(_Safe(ctx))
    out = re.sub(r"[ \t]{2,}", " ", out)
    return "\n".join(line.rstrip() for line in out.splitlines()).strip()


def run_due(today: date | None = None) -> list[dict]:
    today = today or date.today()
    tomorrow = today + timedelta(days=1)
    sent: list[dict] = []

    for kind, on_date, tpl_col in (
        ("precheckin", today.isoformat(), "tpl_precheckin"),
        ("checkout", tomorrow.isoformat(), "tpl_checkout"),
    ):
        for res in db.reservations_due(kind, on_date):
            if not res["guest_phone"]:
                continue
            prop = db.get_property(res["property_id"])
            tpl = prop[tpl_col] or ""
            if not tpl.strip():
                continue
            body = render_template(tpl, res, prop)
            if send_whatsapp(res["guest_phone"], body):
                db.mark_reservation_sent(res["id"], kind)
                db.log_message(res["property_id"], res["guest_name"] or "Hóspede",
                               f"[automática: {kind}]", body, "proactive", kind, None)
                sent.append({"kind": kind, "reservation": res["id"],
                             "to": res["guest_phone"], "body": body})
    return sent


if __name__ == "__main__":
    db.init_db()
    results = run_due()
    if not results:
        print("Nada para enviar agora.")
    for r in results:
        print(f"[{r['kind']}] -> {r['to']}\n{r['body']}\n")
    sys.exit(0)
