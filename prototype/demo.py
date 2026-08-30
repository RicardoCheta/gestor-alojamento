"""
Resets the database and seeds a demo account with sample data, then runs the
sample guest messages end to end.

    python3 demo.py          # offline keyword classifier
    python3 demo.py --llm    # use Claude (needs ANTHROPIC_API_KEY + `pip install anthropic`)

Seeded logins:
  host  :  demo@example.com   /  demo1234
  admin :  admin@example.com  /  admin1234   (client management console)

Any real admin accounts created with create_admin.py are preserved across runs.
"""

import sys

from werkzeug.security import generate_password_hash

import db
import seed_demo
from manager import format_ticket, respond

USE_LLM = "--llm" in sys.argv

DEMO_EMAIL = seed_demo.DEMO_EMAIL
DEMO_PASSWORD = seed_demo.DEMO_PASSWORD
DEMO_ADMIN_EMAIL = "admin@example.com"
DEMO_ADMIN_PASSWORD = "admin1234"

CONVERSATIONS = [
    ("14B", "Ana",   "Olá! Onde está o WiFi?"),
    ("14B", "Ana",   "Como ligo o ar condicionado?"),
    ("14B", "Marco", "Onde posso estacionar?"),
    ("14B", "Marco", "Posso fazer check-out às 14h?"),
    ("14B", "Sofia", "O chuveiro não funciona — está a sair água para todo o lado."),
    ("3A",  "Tomás", "Não há água quente esta manhã."),
    ("3A",  "Tomás", "Qual é a palavra-passe do WiFi?"),
    ("3A",  "Lena",  "Uma das lâmpadas da cozinha fundiu."),
]


def _existing_admins() -> list[dict]:
    if not db.DB_PATH.exists():
        return []
    try:
        with db.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT email, password_hash, name FROM hosts WHERE role = 'admin'")]
    except Exception:
        return []


def seed() -> dict:
    keep_admins = _existing_admins()
    db.reset_db()

    # Re-create any real admin accounts, then a demo admin if none exists.
    restored = set()
    for a in keep_admins:
        db.create_host(a["email"], a["password_hash"], a["name"] or "Admin",
                       role="admin", email_verified=True)
        restored.add(a["email"].lower())
    if DEMO_ADMIN_EMAIL not in restored:
        db.create_host(DEMO_ADMIN_EMAIL, generate_password_hash(DEMO_ADMIN_PASSWORD),
                       "Demo Admin", role="admin", email_verified=True)

    host_id = seed_demo.seed_demo_data()
    return {p["unit"]: p for p in db.list_properties(host_id)}


def main() -> None:
    props = seed()

    print(f"Gestor de Alojamento — demo  (modo {'IA' if USE_LLM else 'offline'})")
    print(f"Anfitrião:  {DEMO_EMAIL}  /  {DEMO_PASSWORD}")
    print(f"Admin:      {DEMO_ADMIN_EMAIL}  /  {DEMO_ADMIN_PASSWORD}")
    print("=" * 64)

    for unit, guest, message in CONVERSATIONS:
        result = respond(props[unit], message, guest_name=guest, use_llm=USE_LLM,
                         dispatch_enabled=True)
        print(f"\n[{unit}] {guest}: {message}")
        print(f"   🤖 {result.reply}")
        if result.ticket:
            print()
            for line in format_ticket(result.ticket).splitlines():
                print(f"   {line}")

    host_id = list(props.values())[0]["host_id"]
    tickets = db.list_tickets(host_id)
    print("\n" + "=" * 64)
    print(f"{len(tickets)} pedido(s) criado(s)")
    for t in tickets:
        who = t["worker_name"] or "sem atribuição"
        prio = db.PRIORITY_LABELS.get(t["priority"], t["priority"])
        cat = db.CATEGORY_LABELS.get(t["category"], t["category"])
        print(f"  T-{t['id']:04d}  {t['unit']:>4}  {prio:<6}  {cat:<15}  → {who}")

    # Simular um prestador a responder "concluído" ao trabalho mais recente.
    import dispatch
    marta = next((w for w in db.list_workers(host_id) if "canaliz" in w["name"].lower()), None)
    if marta and db.latest_open_ticket_for_worker(marta["id"]):
        print(f"\n[prestador] {marta['name']}: concluído")
        print(f"   🤖 {dispatch.handle_worker_reply(marta, 'concluído')}")

    # Ligar a fila de revisão e deixar 2 rascunhos por aprovar.
    db.set_host_feature(host_id, "feat_review", True)
    by_unit = {p["unit"]: p for p in db.list_properties(host_id)}
    for unit, guest, phone, msg in [
        ("14B", "Rita", "whatsapp:+351920000010", "Podem deixar-me fazer check-out mais tarde, às 13h?"),
        ("3A", "Hugo", "whatsapp:+351920000011", "O comando da TV não funciona."),
    ]:
        respond(by_unit[unit], msg, guest_name=guest, review=True, guest_phone=phone)
    print(f"\n{db.count_pending_reviews(host_id)} resposta(s) na fila de revisão")

    # Mensagens proativas devidas hoje (check-in de hoje + véspera de check-out).
    import scheduler
    sent = scheduler.run_due()
    print("\n" + "=" * 64)
    print(f"{len(sent)} mensagem(ns) proativa(s) enviada(s)")
    for s in sent:
        print(f"\n[{s['kind']}] → {s['to']}\n   {s['body']}")

    print("\nAbrir o painel:  .venv/bin/python dashboard.py")


if __name__ == "__main__":
    main()
