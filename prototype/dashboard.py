"""
Host dashboard for the AI Airbnb Property Manager.

Hosts sign up through the platform, then manage their own properties, tickets
and guest conversations. All data is scoped to the logged-in host.

Run:
    python3 -m venv .venv
    .venv/bin/pip install -r requirements.txt
    .venv/bin/python dashboard.py         # http://localhost:5051

Set FLASK_SECRET_KEY in .env so sessions survive restarts. Run `python demo.py`
once to create a demo account (demo@example.com / demo1234) with sample data.
"""

from __future__ import annotations

import html
import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from flask import Flask, redirect, request, url_for
from werkzeug.middleware.proxy_fix import ProxyFix

import auth
import db
from auth import login_required

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.secret_key = auth.secret_key()
app.permanent_session_lifetime = timedelta(days=7)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Behind HTTPS in production; APP_BASE_URL is set there.
    SESSION_COOKIE_SECURE=os.environ.get("APP_BASE_URL", "").startswith("https://"),
)

db.init_db()


def _bootstrap_admin() -> None:
    """Cria uma conta de admin a partir de BOOTSTRAP_ADMIN=email:password.

    Corre no arranque; só cria se ainda não existir nenhum admin. Serve para
    plataformas sem shell (Render free). Depois de entrar, remove a variável.
    """
    raw = os.environ.get("BOOTSTRAP_ADMIN", "")
    if not raw or ":" not in raw:
        return
    from werkzeug.security import generate_password_hash
    email, pw = raw.split(":", 1)
    email, pw = email.strip(), pw.strip()
    with db.connect() as conn:
        has_admin = conn.execute(
            "SELECT 1 FROM hosts WHERE role = 'admin' LIMIT 1").fetchone()
    if has_admin or not email or len(pw) < 8:
        return
    existing = db.get_host_by_email(email)
    if existing:
        db.set_password(existing["id"], generate_password_hash(pw))
        with db.connect() as conn:
            conn.execute("UPDATE hosts SET role='admin', status='active', "
                         "email_verified=1 WHERE id = ?", (existing["id"],))
        print(f"[bootstrap] {email} promovido a admin", flush=True)
    else:
        db.create_host(email, generate_password_hash(pw), "Admin",
                       role="admin", email_verified=True)
        print(f"[bootstrap] conta de admin {email} criada", flush=True)


_bootstrap_admin()

_scheduler_started = False


def _proactive_loop():
    import scheduler
    while True:
        try:
            scheduler.run_due()
        except Exception as exc:  # noqa: BLE001 — prototype: keep the loop alive
            print(f"[scheduler] {exc}", flush=True)
        time.sleep(600)


def _start_scheduler() -> None:
    """Run the proactive-message loop in a background thread (idempotent).

    Local dev (`python dashboard.py`) turns this on automatically. Under a WSGI
    server it stays OFF unless RUN_SCHEDULER=1 — in production run `scheduler.py`
    as a cron job instead, so it fires exactly once regardless of worker count.
    """
    global _scheduler_started
    if _scheduler_started:
        return
    _scheduler_started = True
    threading.Thread(target=_proactive_loop, daemon=True).start()


# Under a WSGI server (gunicorn), opt in with RUN_SCHEDULER=1. Never here for
# `python dashboard.py` — the __main__ block handles that (reloader-safe).
if __name__ != "__main__" and os.environ.get("RUN_SCHEDULER", "").lower() in ("1", "true", "yes"):
    _start_scheduler()

PRIORITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}
STATUS_OPTIONS = ["open", "in progress", "done"]
KIND_LABELS = {"answer": "resposta", "ticket": "pedido", "forwarded": "encaminhada",
               "proactive": "automática", "held": "em revisão"}
FIELD_LABELS = [
    ("name", "Nome do alojamento", "input"),
    ("address", "Morada", "input"),
    ("wifi_network", "Rede WiFi", "input"),
    ("wifi_password", "Palavra-passe WiFi", "input"),
    ("wifi_notes", "Notas sobre o WiFi", "input"),
    ("ac_instructions", "Instruções de ar condicionado / aquecimento", "textarea"),
    ("parking", "Estacionamento", "textarea"),
    ("checkin", "Check-in", "textarea"),
    ("checkout", "Check-out", "textarea"),
    ("trash", "Lixo / reciclagem", "textarea"),
    ("house_rules", "Regras da casa", "textarea"),
    ("emergency_contact", "Contacto de emergência", "input"),
]
PUBLIC_ENDPOINTS = {"login", "signup", "verify_email", "forgot_password",
                    "reset_password_page", "stripe_webhook", "static"}


HOST_ONLY_ENDPOINTS = {"overview", "tickets", "properties", "inbox", "workers",
                       "new_property", "edit_property", "update_ticket", "import_property",
                       "new_worker", "edit_worker", "assign_ticket_route",
                       "reservations", "new_reservation", "edit_reservation",
                       "billing", "request_plan_route", "billing_checkout", "billing_portal",
                       "review", "review_approve", "review_discard"}


@app.before_request
def require_login():
    if request.endpoint in PUBLIC_ENDPOINTS:
        return None
    host = auth.current_host()
    if not host:
        return redirect(url_for("login", next=request.path))
    # Admins have no properties of their own — send them to the client console.
    if host["role"] == "admin" and request.endpoint in HOST_ONLY_ENDPOINTS:
        return redirect(url_for("admin_home"))
    return None


# --------------------------------------------------------------------------- #
# Rendering helpers
# --------------------------------------------------------------------------- #

def e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def ago(iso: str) -> str:
    try:
        then = datetime.fromisoformat(iso)
    except (ValueError, TypeError):
        return str(iso)
    secs = (datetime.now(timezone.utc) - then).total_seconds()
    for label, size in (("d", 86400), ("h", 3600), ("min", 60)):
        if secs >= size:
            return f"há {int(secs // size)} {label}"
    return "agora mesmo"


def page(title: str, body: str) -> str:
    host = auth.current_host()
    if host and host["role"] == "admin":
        tabs = (("admin_home", "Clientes"), ("account", "Conta"))
    elif host:
        n_review = db.count_pending_reviews(host["id"]) if host["feat_review"] else 0
        review_label = f"A rever ({n_review})" if n_review else "A rever"
        tabs = [("overview", "Resumo"), ("tickets", "Pedidos"),
                ("properties", "Alojamentos"), ("reservations", "Reservas"),
                ("workers", "Prestadores"), ("inbox", "Mensagens"),
                ("billing", "Faturação"), ("account", "Conta")]
        if host["feat_review"]:
            tabs.insert(1, ("review", review_label))
    else:
        tabs = ()

    if host:
        nav = "".join(
            f'<a href="{url_for(ep)}" class="{"on" if request.endpoint == ep else ""}">{label}</a>'
            for ep, label in tabs
        )
        role_tag = ' <span class="pill">admin</span>' if host["role"] == "admin" else ""
        nav += (f'<span class="spacer"></span>'
                f'<span class="who">{e(host["email"])}{role_tag}</span>'
                f'<a href="{url_for("logout")}">Sair</a>')
    else:
        nav = ""

    banner = ""
    if host and host["role"] == "host" and not host["email_verified"]:
        banner = (f"<div class='warn'>Confirme o seu email. "
                  f"<a href='{url_for('resend_verification')}'>Reenviar o link</a>.</div>")

    return f"""<!doctype html><html lang="pt"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} — Gestor de Alojamento</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font: 15px/1.5 -apple-system, Segoe UI, Roboto, sans-serif;
         color: #1c1e21; background: #f0f2f5; }}
  header {{ background: #fff; border-bottom: 1px solid #dcdfe3; padding: 0 24px;
           display: flex; align-items: center; gap: 28px; }}
  header h1 {{ font-size: 16px; margin: 0; padding: 16px 0; white-space: nowrap; }}
  header nav {{ display: flex; align-items: center; flex: 1; }}
  header nav a {{ display: inline-block; padding: 18px 4px; margin-right: 18px;
                 color: #606770; text-decoration: none; border-bottom: 3px solid transparent;
                 white-space: nowrap; }}
  header nav a.on {{ color: #1877f2; border-color: #1877f2; }}
  header nav .spacer {{ flex: 1; }}
  header nav .who {{ color: #606770; font-size: 13px; margin-right: 14px; }}
  main {{ max-width: 920px; margin: 24px auto; padding: 0 24px; }}
  h2 {{ font-size: 20px; margin: 0 0 16px; }}
  h3 {{ margin: 22px 0 8px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px,1fr));
           gap: 16px; margin-bottom: 28px; }}
  .card {{ background: #fff; border: 1px solid #dcdfe3; border-radius: 10px; padding: 16px; }}
  .card .n {{ font-size: 28px; font-weight: 700; }}
  .card .l {{ color: #606770; font-size: 13px; }}
  table {{ width: 100%; border-collapse: collapse; background: #fff;
          border: 1px solid #dcdfe3; border-radius: 10px; overflow: hidden; }}
  th, td {{ text-align: left; padding: 10px 14px; border-bottom: 1px solid #eceef0;
           vertical-align: top; }}
  th {{ background: #f7f8fa; font-size: 13px; color: #606770; }}
  tr:last-child td {{ border-bottom: none; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 999px;
           font-size: 12px; font-weight: 600; }}
  .High {{ background: #ffe5e5; color: #c0322b; }}
  .Medium {{ background: #fff2d8; color: #a5680a; }}
  .Low {{ background: #e6f0ff; color: #1a56c4; }}
  .k-answer {{ color: #1a7f37; }}
  .k-ticket {{ color: #c0322b; }}
  .k-forwarded {{ color: #8a6d3b; }}
  .k-proactive {{ color: #1a56c4; }}
  .k-held {{ color: #a5680a; }}
  .bar {{ background: #1877f2; height: 10px; border-radius: 5px; }}
  .pill {{ background: #eceef0; border-radius: 999px; padding: 2px 8px; font-size: 12px; }}
  code {{ background: #00000010; padding: 1px 5px; border-radius: 4px; }}
  form.inline {{ display: inline; }}
  select, input, textarea {{ font: inherit; padding: 8px 10px; border: 1px solid #ccd0d5;
           border-radius: 8px; width: 100%; background: #fff; }}
  textarea {{ min-height: 70px; resize: vertical; }}
  .field {{ margin-bottom: 16px; }}
  .field label {{ display: block; font-weight: 600; font-size: 13px; margin-bottom: 6px; }}
  button {{ font: inherit; font-weight: 600; padding: 9px 16px; border: none;
           border-radius: 8px; background: #1877f2; color: #fff; cursor: pointer; }}
  button.ghost {{ background: #e4e6eb; color: #1c1e21; }}
  button.danger {{ background: #fbe4e2; color: #c0322b; }}
  .muted {{ color: #606770; }}
  .row-msg {{ max-width: 360px; }}
  .auth {{ max-width: 360px; margin: 7vh auto; background: #fff; border: 1px solid #dcdfe3;
          border-radius: 12px; padding: 28px; }}
  .auth h2 {{ margin-top: 0; }}
  .auth .alt {{ margin-top: 16px; font-size: 14px; }}
  .err {{ background: #ffe5e5; color: #c0322b; padding: 8px 12px; border-radius: 8px;
         font-size: 14px; margin-bottom: 14px; }}
  .ok {{ background: #e4f5e9; color: #1a7f37; padding: 8px 12px; border-radius: 8px;
        font-size: 14px; margin-bottom: 14px; }}
  .warn {{ background: #fff4d6; border: 1px solid #f0d68a; color: #7a5c00;
          padding: 10px 14px; border-radius: 8px; margin-bottom: 18px; font-size: 14px; }}
  .toolbar {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; }}
  .toggle-row {{ display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }}
  .toggle-row input {{ width: auto; }}
  .status-active {{ color: #1a7f37; font-weight: 600; }}
  .status-suspended {{ color: #c0322b; font-weight: 600; }}
</style></head><body>
<header><h1>🏡 Gestor de Alojamento</h1><nav>{nav}</nav></header>
<main>{banner}{body}</main></body></html>"""


def _host_id() -> int:
    return auth.current_host()["id"]


# --------------------------------------------------------------------------- #
# Auth pages
# --------------------------------------------------------------------------- #

def _safe_next() -> str:
    nxt = request.args.get("next") or request.form.get("next") or ""
    return nxt if nxt.startswith("/") and not nxt.startswith("//") else "/"


def _home_for(host) -> str:
    return url_for("admin_home") if host["role"] == "admin" else url_for("overview")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if auth.current_host():
        return redirect("/")
    err = ""
    email = name = ""
    if request.method == "POST":
        email = request.form.get("email", "")
        name = request.form.get("name", "")
        pw = request.form.get("password", "")
        pw2 = request.form.get("password2", "")
        err = auth.validate_signup(email, pw, pw2) or ""
        if not err:
            auth.signup(email, pw, name)
            return redirect(url_for("overview"))

    err_html = f"<div class='err'>{e(err)}</div>" if err else ""
    body = f"""<div class="auth">
      <h2>Criar conta</h2>{err_html}
      <form method="post">
        <div class="field"><label>O seu nome</label>
          <input name="name" value="{e(name)}" autocomplete="name"></div>
        <div class="field"><label>Email</label>
          <input name="email" type="email" value="{e(email)}" autocomplete="email" required></div>
        <div class="field"><label>Palavra-passe</label>
          <input name="password" type="password" autocomplete="new-password" required></div>
        <div class="field"><label>Repita a palavra-passe</label>
          <input name="password2" type="password" autocomplete="new-password" required></div>
        <button type="submit">Criar conta</button>
      </form>
      <div class="alt muted">Já tem conta? <a href="{url_for('login')}">Entrar</a></div>
    </div>"""
    return page("Criar conta", body)


@app.route("/login", methods=["GET", "POST"])
def login():
    host = auth.current_host()
    if host:
        return redirect(_home_for(host))
    err = ""
    email = ""
    if request.method == "POST":
        email = request.form.get("email", "")
        if auth.locked_out():
            err = "Demasiadas tentativas. Aguarde um minuto e tente de novo."
        else:
            outcome = auth.try_login(email, request.form.get("password", ""))
            if outcome is True:
                return redirect(_safe_next())
            if outcome == auth.LOGIN_SUSPENDED:
                err = "Esta conta está suspensa. Contacte o suporte."
            else:
                err = "Email ou palavra-passe incorretos."

    err_html = f"<div class='err'>{e(err)}</div>" if err else ""
    body = f"""<div class="auth">
      <h2>Entrar</h2>{err_html}
      <form method="post">
        <input type="hidden" name="next" value="{e(_safe_next())}">
        <div class="field"><label>Email</label>
          <input name="email" type="email" value="{e(email)}" autocomplete="email" autofocus required></div>
        <div class="field"><label>Palavra-passe</label>
          <input name="password" type="password" autocomplete="current-password" required></div>
        <button type="submit">Entrar</button>
      </form>
      <div class="alt muted"><a href="{url_for('forgot_password')}">Esqueceu-se da palavra-passe?</a>
        &nbsp;·&nbsp; É novo por aqui? <a href="{url_for('signup')}">Criar conta</a></div>
    </div>"""
    return page("Entrar", body)


@app.route("/logout")
def logout():
    auth.logout()
    return redirect(url_for("login"))


@app.route("/verify/<token>")
def verify_email(token: str):
    ok = auth.verify_email(token)
    msg = ("<div class='ok'>Email confirmado. Obrigado!</div>" if ok
           else "<div class='err'>Esse link de confirmação é inválido ou já foi usado.</div>")
    return page("Confirmar email",
                f"<div class='auth'><h2>Confirmação de email</h2>{msg}"
                f"<div class='alt'><a href='/'>Ir para o painel</a></div></div>")


def _mail_note() -> str:
    import mailer
    return ("Verifique a sua caixa de entrada." if mailer.configured()
            else "O link foi impresso na consola do servidor (email não configurado).")


@app.route("/resend-verification")
@login_required
def resend_verification():
    auth.resend_verification(auth.current_host())
    return page("Link enviado",
                "<div class='auth'><h2>Confirme o seu email</h2>"
                f"<div class='ok'>Enviámos um novo link de confirmação. {_mail_note()}</div>"
                "<div class='alt'><a href='/'>Voltar ao painel</a></div></div>")


@app.route("/forgot", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        auth.request_password_reset(request.form.get("email", ""))
        return page("Link enviado",
                    "<div class='auth'><h2>Verifique o seu email</h2>"
                    f"<div class='ok'>Se esse email tiver uma conta, enviámos um "
                    f"link de recuperação. {_mail_note()}</div>"
                    f"<div class='alt'><a href='{url_for('login')}'>Voltar a entrar</a></div></div>")
    body = f"""<div class="auth"><h2>Recuperar a palavra-passe</h2>
      <form method="post">
        <div class="field"><label>Email</label>
          <input name="email" type="email" autocomplete="email" autofocus required></div>
        <button type="submit">Enviar link de recuperação</button>
      </form>
      <div class="alt muted"><a href="{url_for('login')}">Voltar a entrar</a></div>
    </div>"""
    return page("Recuperar palavra-passe", body)


@app.route("/reset/<token>", methods=["GET", "POST"], endpoint="reset_password_page")
def reset_password_page(token: str):
    err = ""
    if request.method == "POST":
        pw = request.form.get("password", "")
        pw2 = request.form.get("password2", "")
        if pw != pw2:
            err = "As duas palavras-passe não coincidem."
        else:
            err = auth.reset_password(token, pw) or ""
        if not err:
            return page("Palavra-passe alterada",
                        "<div class='auth'><h2>Palavra-passe alterada</h2>"
                        "<div class='ok'>Já pode entrar com a nova palavra-passe.</div>"
                        f"<div class='alt'><a href='{url_for('login')}'>Entrar</a></div></div>")
    err_html = f"<div class='err'>{e(err)}</div>" if err else ""
    body = f"""<div class="auth"><h2>Escolha uma nova palavra-passe</h2>{err_html}
      <form method="post">
        <div class="field"><label>Nova palavra-passe</label>
          <input name="password" type="password" autocomplete="new-password" autofocus required></div>
        <div class="field"><label>Repita a nova palavra-passe</label>
          <input name="password2" type="password" autocomplete="new-password" required></div>
        <button type="submit">Guardar nova palavra-passe</button>
      </form>
    </div>"""
    return page("Recuperar palavra-passe", body)


@app.route("/account", methods=["GET", "POST"])
@login_required
def account():
    host = auth.current_host()
    msg = ""
    if request.method == "POST":
        cur = request.form.get("current", "")
        new = request.form.get("password", "")
        new2 = request.form.get("password2", "")
        from werkzeug.security import check_password_hash, generate_password_hash
        if not check_password_hash(host["password_hash"], cur):
            msg = "<div class='err'>A palavra-passe atual está errada.</div>"
        elif len(new) < auth.MIN_PASSWORD_LEN:
            msg = f"<div class='err'>A nova palavra-passe tem de ter pelo menos {auth.MIN_PASSWORD_LEN} caracteres.</div>"
        elif new != new2:
            msg = "<div class='err'>As duas novas palavras-passe não coincidem.</div>"
        else:
            db.set_password(host["id"], generate_password_hash(new))
            msg = "<div class='ok'>Palavra-passe atualizada.</div>"

    papel = "administrador" if host["role"] == "admin" else "anfitrião"
    body = f"""<h2>Conta</h2>{msg}
    <p class="muted">{e(host['email'])} · perfil: {papel}
      {'· plano: ' + e(db.PLANS[host['plan']]['label']) if host['role'] == 'host' else ''}</p>
    <h3>Alterar palavra-passe</h3>
    <form method="post" style="max-width:360px">
      <div class="field"><label>Palavra-passe atual</label>
        <input name="current" type="password" autocomplete="current-password" required></div>
      <div class="field"><label>Nova palavra-passe</label>
        <input name="password" type="password" autocomplete="new-password" required></div>
      <div class="field"><label>Repita a nova palavra-passe</label>
        <input name="password2" type="password" autocomplete="new-password" required></div>
      <button type="submit">Atualizar palavra-passe</button>
    </form>"""
    return page("Conta", body)


# --------------------------------------------------------------------------- #
# Dashboard pages
# --------------------------------------------------------------------------- #

@app.route("/")
@login_required
def overview():
    hid = _host_id()
    msgs = [dict(m) for m in db.list_messages(hid)]
    tickets = [dict(t) for t in db.list_tickets(hid)]
    props = db.list_properties(hid)

    today = datetime.now(timezone.utc).date().isoformat()
    msgs_today = [m for m in msgs if str(m["created_at"])[:10] == today]
    inbound = [m for m in msgs if m["kind"] in ("answer", "ticket", "forwarded")]
    answered = [m for m in inbound if m["kind"] == "answer"]
    auto_rate = round(100 * len(answered) / len(inbound)) if inbound else 0
    open_tickets = [t for t in tickets if t["status"] != "done"]

    if not props:
        body = f"""<h2>Bem-vindo 👋</h2>
        <p>Adicione o seu primeiro alojamento para o assistente começar a responder aos hóspedes.</p>
        <a href="{url_for('new_property')}"><button>Adicionar alojamento</button></a>"""
        return page("Resumo", body)

    host = auth.current_host()
    review_card = ""
    if host["feat_review"]:
        n = db.count_pending_reviews(host["id"])
        review_card = (f"<a class='card' href='{url_for('review')}' "
                       f"style=\"text-decoration:none;color:inherit;"
                       f"{'border-color:#1877f2' if n else ''}\">"
                       f"<div class='n'>{n}</div><div class='l'>A rever</div></a>")
    cards = f"""<div class="cards">
      <div class="card"><div class="n">{len(msgs_today)}</div><div class="l">Mensagens hoje</div></div>
      <div class="card"><div class="n">{auto_rate}%</div><div class="l">Respondidas por IA</div></div>
      <div class="card"><div class="n">{len(open_tickets)}</div><div class="l">Pedidos em aberto</div></div>
      {review_card}
      <div class="card"><div class="n">{len(props)}</div><div class="l">Alojamentos</div></div>
    </div>"""

    rows = "".join(
        f"<tr><td class='muted'>{ago(m['created_at'])}</td><td>{e(m['unit'])}</td>"
        f"<td>{e(m['guest'])}</td><td class='row-msg'>{e(m['body'])}</td>"
        f"<td class='k-{m['kind']}'>{e(KIND_LABELS.get(m['kind'], m['kind']))}</td></tr>"
        for m in reversed(msgs[-12:])
    ) or "<tr><td colspan='5' class='muted'>Ainda sem mensagens de hóspedes.</td></tr>"

    body = f"""<h2>Resumo</h2>{cards}
    <h3>Atividade recente</h3>
    <table><tr><th>Quando</th><th>Unidade</th><th>Hóspede</th><th>Mensagem</th><th>Tratada</th></tr>
    {rows}</table>"""
    return page("Resumo", body)


@app.route("/tickets")
@login_required
def tickets():
    hid = _host_id()
    items = sorted(
        (dict(t) for t in db.list_tickets(hid)),
        key=lambda t: (t["status"] == "done", PRIORITY_ORDER.get(t["priority"], 9),
                       t["created_at"]),
    )
    workers = db.list_workers(hid, active_only=True)
    rows = ""
    for t in items:
        opts = "".join(
            f"<option value='{s}' {'selected' if t['status'] == s else ''}>"
            f"{db.STATUS_LABELS[s]}</option>"
            for s in STATUS_OPTIONS
        )
        worker_opts = '<option value="">— sem atribuição —</option>' + "".join(
            f"<option value='{w['id']}' {'selected' if t['assigned_worker_id'] == w['id'] else ''}>"
            f"{e(w['name'])}</option>"
            for w in workers
        )
        prio = db.PRIORITY_LABELS.get(t["priority"], t["priority"])
        cat = db.CATEGORY_LABELS.get(t["category"], t["category"])
        rows += f"""<tr>
          <td>T-{t['id']:04d}</td>
          <td>{e(t['unit'])}</td>
          <td>{e(t['issue'])}<div class='muted'>{e(cat)} · {e(t['guest'])} · {ago(t['created_at'])}</div></td>
          <td><span class="badge {e(t['priority'])}">{e(prio)}</span></td>
          <td><form class="inline" method="post" action="{url_for('assign_ticket_route', ticket_id=t['id'])}">
              <select name="worker_id" onchange="this.form.submit()">{worker_opts}</select></form></td>
          <td><form class="inline" method="post" action="{url_for('update_ticket', ticket_id=t['id'])}">
              <select name="status" onchange="this.form.submit()">{opts}</select></form></td>
        </tr>"""
    rows = rows or "<tr><td colspan='6' class='muted'>Ainda sem pedidos.</td></tr>"
    hint = ("" if workers else
            f"<p class='warn'>Adicione <a href='{url_for('workers')}'>prestadores</a> "
            "para enviar os pedidos automaticamente por WhatsApp.</p>")
    body = f"""<h2>Pedidos de manutenção</h2>{hint}
    <table><tr><th>ID</th><th>Unidade</th><th>Problema</th><th>Prioridade</th><th>Atribuído</th><th>Estado</th></tr>
    {rows}</table>
    <p class="muted">Primeiro os abertos, depois por prioridade. As alterações são
    guardadas de imediato. Reatribuir um pedido envia mensagem ao novo prestador.</p>"""
    return page("Pedidos", body)


@app.route("/tickets/<int:ticket_id>/status", methods=["POST"])
@login_required
def update_ticket(ticket_id: int):
    status = request.form.get("status", "")
    if status in STATUS_OPTIONS:
        db.set_ticket_status(ticket_id, _host_id(), status)
    return redirect(url_for("tickets"))


@app.route("/tickets/<int:ticket_id>/assign", methods=["POST"], endpoint="assign_ticket_route")
@login_required
def assign_ticket_route(ticket_id: int):
    hid = _host_id()
    raw = request.form.get("worker_id", "").strip()
    worker_id = int(raw) if raw.isdigit() else None
    if worker_id and db.get_worker(worker_id, hid) is None:
        worker_id = None
    if db.assign_ticket(ticket_id, worker_id, host_id=hid) and worker_id:
        ticket = db.get_ticket(ticket_id)
        prop = db.get_property(ticket["property_id"])
        import dispatch
        dispatch.notify_worker_manual(ticket, prop, db.get_worker(worker_id, hid))
    return redirect(url_for("tickets"))


@app.route("/properties")
@login_required
def properties():
    host = auth.current_host()
    rows = "".join(
        f"<tr><td>{e(p['unit'])}</td><td>{e(p['name'])}</td>"
        f"<td><code>{e(p['code'])}</code></td>"
        f"<td><a href='{url_for('edit_property', property_id=p['id'])}'>Editar</a></td></tr>"
        for p in db.list_properties(host["id"])
    ) or "<tr><td colspan='4' class='muted'>Ainda sem alojamentos.</td></tr>"

    limit = db.PLANS[host["plan"]]["max_properties"]
    at_limit = db.property_limit_reached(host)
    add_btn = ("<span class='muted'>Limite do plano atingido — faça upgrade para adicionar mais</span>"
               if at_limit else
               f'<a href="{url_for("new_property")}"><button>Adicionar alojamento</button></a>')
    plan_note = (f"<p class='muted'>Plano: <b>{e(db.PLANS[host['plan']]['label'])}</b>"
                 f"{f' · {limit} alojamento' if limit == 1 else f' · até {limit} alojamentos' if limit else ' · alojamentos ilimitados'}.</p>")
    body = f"""<div class="toolbar"><h2>Alojamentos</h2>{add_btn}</div>
    {plan_note}
    <table><tr><th>Unidade</th><th>Nome</th><th>Código do hóspede</th><th></th></tr>{rows}</table>
    <p class="muted">O <b>código do hóspede</b> é o que o hóspede envia por WhatsApp
    para ligar a conversa ao alojamento certo.</p>"""
    return page("Alojamentos", body)


def _property_form(action: str, prop: dict, heading: str, extra: str = "") -> str:
    unit_val = e(prop.get("unit", ""))
    fields = f"""<div class="field"><label>Unidade / etiqueta interna (ex.: 14B)</label>
      <input name="unit" value="{unit_val}" required></div>"""
    for key, label, kind in FIELD_LABELS:
        val = e(prop.get(key, ""))
        if kind == "input":
            fields += f'<div class="field"><label>{label}</label><input name="{key}" value="{val}"></div>'
        else:
            fields += f'<div class="field"><label>{label}</label><textarea name="{key}">{val}</textarea></div>'

    tpl_pre = e(prop.get("tpl_precheckin", "") or db.DEFAULT_TPL_PRECHECKIN)
    tpl_out = e(prop.get("tpl_checkout", "") or db.DEFAULT_TPL_CHECKOUT)
    templates = f"""
    <h3 style="margin-top:28px">Mensagens automáticas</h3>
    <p class="muted">Enviadas por WhatsApp ao hóspede (se houver uma reserva com
    telefone). Variáveis: <code>{{hospede}}</code> <code>{{morada}}</code>
    <code>{{entrada}}</code> <code>{{saida}}</code> <code>{{checkin_hora}}</code>
    <code>{{checkout_hora}}</code> <code>{{wifi_rede}}</code>
    <code>{{wifi_senha}}</code> <code>{{codigo}}</code></p>
    <div class="field"><label>No dia do check-in</label>
      <textarea name="tpl_precheckin">{tpl_pre}</textarea></div>
    <div class="field"><label>Na véspera do check-out</label>
      <textarea name="tpl_checkout">{tpl_out}</textarea></div>"""

    return f"""<h2>{e(heading)}</h2>{extra}
    <form method="post" action="{action}">{fields}{templates}
      <button type="submit">Guardar</button>
      <a href="{url_for('properties')}"><button type="button" class="ghost">Cancelar</button></a>
    </form>"""


def _import_box() -> str:
    return f"""<details style="margin-bottom:20px">
      <summary style="cursor:pointer; font-weight:600">✨ Pré-preencher a partir do texto do seu anúncio Airbnb</summary>
      <form method="post" action="{url_for('import_property')}" style="margin-top:12px">
        <div class="field">
          <textarea name="listing_text" style="min-height:140px"
            placeholder="Cole aqui a descrição do seu anúncio Airbnb e/ou o manual da casa…"></textarea>
        </div>
        <button type="submit">Extrair campos</button>
        <span class="muted">Reveja tudo antes de guardar.</span>
      </form>
    </details>"""


@app.route("/properties/new", methods=["GET", "POST"])
@login_required
def new_property():
    err = ""
    form = {}
    if request.method == "POST":
        form = {k: request.form.get(k, "") for k in
                ["unit", *db.PROPERTY_FIELDS, "tpl_precheckin", "tpl_checkout"]}
        if db.property_limit_reached(auth.current_host()):
            err = "Atingiu o limite de alojamentos do seu plano — faça upgrade para adicionar mais."
        elif not form["unit"].strip():
            err = "A etiqueta da unidade é obrigatória."
        else:
            try:
                db.create_property(_host_id(), form["unit"], form)
                return redirect(url_for("properties"))
            except Exception:
                err = "Já tem um alojamento com essa etiqueta de unidade."
    extra = (f"<div class='err'>{e(err)}</div>" if err else "") + _import_box()
    return page("Novo alojamento",
                _property_form(url_for("new_property"), form, "Adicionar alojamento", extra))


@app.route("/properties/import", methods=["POST"])
@login_required
def import_property():
    import listing_import
    fields, source = listing_import.extract_fields(request.form.get("listing_text", ""))
    filled = [k for k, v in fields.items() if v]
    note = (f"<div class='ok'>Preenchi {len(filled)} campo(s) "
            f"{'com IA' if source == 'ai' else 'a partir de padrões no texto'}"
            f"{' — defina ANTHROPIC_API_KEY para melhores resultados' if source != 'ai' else ''}. "
            f"Verifique tudo, adicione a etiqueta da unidade e Guarde.</div>")
    return page("Rever alojamento importado",
                _property_form(url_for("new_property"), fields, "Adicionar alojamento", note))


@app.route("/properties/<int:property_id>", methods=["GET", "POST"])
@login_required
def edit_property(property_id: int):
    prop = db.get_property(property_id, _host_id())
    if prop is None:
        return page("Não encontrado", "<h2>Alojamento não encontrado</h2>"), 404

    if request.method == "POST":
        if request.form.get("_action") == "delete":
            db.delete_property(property_id, _host_id())
            return redirect(url_for("properties"))
        fields = {k: request.form.get(k, "") for k in
                  ["unit", *db.PROPERTY_FIELDS, "tpl_precheckin", "tpl_checkout"]}
        try:
            db.update_property(property_id, _host_id(), fields)
            return redirect(url_for("edit_property", property_id=property_id, saved=1))
        except Exception:
            prop = db.get_property(property_id, _host_id())
            extra = "<div class='err'>Essa etiqueta de unidade já é usada noutro dos seus alojamentos.</div>"
            return page("Editar alojamento",
                        _property_form(url_for("edit_property", property_id=property_id),
                                       fields, f"Editar {prop['unit']}", extra))

    saved = "<div class='ok'>Guardado.</div>" if request.args.get("saved") else ""
    guest_help = (f"<p class='muted'>Código do hóspede: <code>{e(prop['code'])}</code> — "
                  f"o hóspede envia isto por WhatsApp para chegar a este alojamento.</p>")
    delete_form = f"""<form method="post" style="margin-top:24px"
        onsubmit="return confirm('Eliminar {e(prop['unit'])} e todos os seus pedidos e mensagens?')">
      <input type="hidden" name="_action" value="delete">
      <button type="submit" class="danger">Eliminar este alojamento</button>
    </form>"""
    body = _property_form(url_for("edit_property", property_id=property_id),
                          dict(prop), f"Editar {prop['unit']} — {prop['name']}",
                          saved + guest_help)
    return page(f"Editar {prop['unit']}", body + delete_form)


# --------------------------------------------------------------------------- #
# Reservas (para mensagens proativas)
# --------------------------------------------------------------------------- #

def _reservation_form(action: str, r: dict, heading: str, props, extra: str = "") -> str:
    opts = "".join(
        f"<option value='{p['id']}' {'selected' if r.get('property_id') == p['id'] else ''}>"
        f"{e(p['unit'])} — {e(p['name'])}</option>" for p in props
    )
    return f"""<h2>{e(heading)}</h2>{extra}
    <form method="post" action="{action}" style="max-width:420px">
      <div class="field"><label>Alojamento</label>
        <select name="property_id" required>{opts}</select></div>
      <div class="field"><label>Nome do hóspede</label>
        <input name="guest_name" value="{e(r.get('guest_name',''))}"></div>
      <div class="field"><label>WhatsApp do hóspede (com indicativo, ex.: +351912345678)</label>
        <input name="guest_phone" value="{e(r.get('guest_phone',''))}"></div>
      <div class="field"><label>Check-in</label>
        <input name="check_in" type="date" value="{e(r.get('check_in',''))}" required></div>
      <div class="field"><label>Check-out</label>
        <input name="check_out" type="date" value="{e(r.get('check_out',''))}" required></div>
      <button type="submit">Guardar</button>
      <a href="{url_for('reservations')}"><button type="button" class="ghost">Cancelar</button></a>
    </form>"""


@app.route("/reservations")
@login_required
def reservations():
    hid = _host_id()
    rows = ""
    for r in db.list_reservations(hid):
        pre = "enviada" if r["precheckin_sent_at"] else "—"
        out = "enviada" if r["checkout_sent_at"] else "—"
        rows += (f"<tr><td>{e(r['unit'])}</td><td>{e(r['guest_name'] or '—')}</td>"
                 f"<td>{e(r['check_in'])} → {e(r['check_out'])}</td>"
                 f"<td class='muted'>{e(r['guest_phone'] or 'sem telefone')}</td>"
                 f"<td class='muted'>chegada: {pre} · saída: {out}</td>"
                 f"<td><a href='{url_for('edit_reservation', res_id=r['id'])}'>Editar</a></td></tr>")
    rows = rows or "<tr><td colspan='6' class='muted'>Ainda sem reservas.</td></tr>"
    has_props = bool(db.list_properties(hid))
    add = (f'<a href="{url_for("new_reservation")}"><button>Adicionar reserva</button></a>'
           if has_props else "<span class='muted'>Adicione um alojamento primeiro</span>")
    body = f"""<div class="toolbar"><h2>Reservas</h2>{add}</div>
    <table><tr><th>Unidade</th><th>Hóspede</th><th>Datas</th><th>WhatsApp</th>
      <th>Mensagens automáticas</th><th></th></tr>{rows}</table>
    <p class="muted">No dia do check-in e na véspera do check-out, o hóspede recebe
    por WhatsApp as mensagens definidas em cada alojamento (secção
    <i>Mensagens automáticas</i>). Requer a funcionalidade <i>Mensagens proativas</i>.</p>"""
    return page("Reservas", body)


def _res_form_values() -> dict:
    return {
        "property_id": int(request.form.get("property_id") or 0),
        "guest_name": request.form.get("guest_name", ""),
        "guest_phone": request.form.get("guest_phone", ""),
        "check_in": request.form.get("check_in", ""),
        "check_out": request.form.get("check_out", ""),
    }


@app.route("/reservations/new", methods=["GET", "POST"])
@login_required
def new_reservation():
    hid = _host_id()
    props = db.list_properties(hid)
    err = ""
    v = {}
    if request.method == "POST":
        v = _res_form_values()
        if db.get_property(v["property_id"], hid) is None:
            err = "Escolha um alojamento válido."
        elif not v["check_in"] or not v["check_out"] or v["check_out"] <= v["check_in"]:
            err = "As datas de check-in e check-out não são válidas."
        else:
            db.create_reservation(v["property_id"], v["guest_name"], v["guest_phone"],
                                  v["check_in"], v["check_out"])
            return redirect(url_for("reservations"))
    extra = f"<div class='err'>{e(err)}</div>" if err else ""
    return page("Nova reserva",
                _reservation_form(url_for("new_reservation"), v, "Adicionar reserva", props, extra))


@app.route("/reservations/<int:res_id>", methods=["GET", "POST"])
@login_required
def edit_reservation(res_id: int):
    hid = _host_id()
    r = db.get_reservation(res_id, hid)
    if r is None:
        return page("Não encontrado", "<h2>Reserva não encontrada</h2>"), 404
    props = db.list_properties(hid)
    if request.method == "POST":
        if request.form.get("_action") == "delete":
            db.delete_reservation(res_id, hid)
            return redirect(url_for("reservations"))
        v = _res_form_values()
        if v["check_in"] and v["check_out"] and v["check_out"] > v["check_in"]:
            db.update_reservation(res_id, hid, v["guest_name"], v["guest_phone"],
                                  v["check_in"], v["check_out"])
        return redirect(url_for("reservations"))
    delete_form = f"""<form method="post" style="margin-top:20px"
        onsubmit="return confirm('Eliminar esta reserva?')">
      <input type="hidden" name="_action" value="delete">
      <button type="submit" class="danger">Eliminar reserva</button></form>"""
    return page("Editar reserva",
                _reservation_form(url_for("edit_reservation", res_id=res_id), dict(r),
                                  "Editar reserva", props) + delete_form)


# --------------------------------------------------------------------------- #
# Faturação (stub — sem pagamentos reais)
# --------------------------------------------------------------------------- #

@app.route("/billing")
@login_required
def billing():
    import billing as bl
    host = auth.current_host()
    plan = db.PLANS[host["plan"]]
    n_props = len(db.list_properties(host["id"]))
    limit = plan["max_properties"]
    limit_txt = f"{n_props} / {limit}" if limit else f"{n_props} (ilimitado)"
    stripe_on = bl.enabled()
    has_sub = host["subscription_status"] in ("active", "trialing", "past_due")

    notes = ""
    if request.args.get("ok"):
        notes += "<div class='ok'>Subscrição ativada. Obrigado!</div>"
    if host["subscription_status"] == "past_due":
        notes += ("<div class='warn'>O último pagamento falhou. Atualize o método "
                  "de pagamento em <b>Gerir subscrição</b> para não perder o serviço.</div>")
    if not stripe_on and host["requested_plan"] and host["requested_plan"] != host["plan"]:
        notes += (f"<div class='warn'>Pedido de mudança para <b>"
                  f"{e(db.PLANS[host['requested_plan']]['label'])}</b> enviado. "
                  f"A nossa equipa trata disto em breve.</div>")

    plan_cards = ""
    for key, p in db.PLANS.items():
        is_current = key == host["plan"]
        mp = p["max_properties"]
        lim = (f"{mp} alojamento" if mp == 1 else
               f"até {mp} alojamentos" if mp else "alojamentos ilimitados")
        if is_current:
            action = "<span class='pill'>plano atual</span>"
        elif stripe_on and has_sub:
            action = (f"<form method='post' action='{url_for('billing_portal')}' class='inline'>"
                      f"<button type='submit' class='ghost'>Mudar para este</button></form>")
        elif stripe_on:
            action = (f"<form method='post' action='{url_for('billing_checkout')}' class='inline'>"
                      f"<input type='hidden' name='plan' value='{key}'>"
                      f"<button type='submit'>Subscrever</button></form>")
        elif key == host["requested_plan"]:
            action = "<span class='muted'>pedido enviado</span>"
        else:
            action = (f"<form method='post' action='{url_for('request_plan_route')}' class='inline'>"
                      f"<input type='hidden' name='plan' value='{key}'>"
                      f"<button type='submit' class='ghost'>Pedir este plano</button></form>")
        plan_cards += f"""<div class="card" style="{'border-color:#1877f2' if is_current else ''}">
          <div class="n">{p['price']}€<span style="font-size:14px;font-weight:400">/mês</span></div>
          <div class="l"><b>{p['label']}</b> · {lim}</div>
          <div style="margin-top:10px">{action}</div>
        </div>"""

    if stripe_on and has_sub:
        manage = (f"<form method='post' action='{url_for('billing_portal')}' style='margin:16px 0'>"
                  f"<button type='submit'>Gerir subscrição</button></form>"
                  "<p class='muted'>Alterar cartão, mudar de plano ou cancelar — no Stripe.</p>")
        foot = ""
    elif stripe_on:
        manage = ""
        foot = "<p class='muted'>Pagamento seguro processado pelo Stripe.</p>"
    else:
        manage = ""
        foot = ("<p class='muted'>Pagamento ainda não ativo nesta instância — pedir "
                "um plano notifica o operador, que o aplica na consola de administração.</p>")

    sub_txt = (f" · estado: <b>{e(host['subscription_status'])}</b>"
               if host['subscription_status'] else "")
    body = f"""<h2>Faturação</h2>{notes}
    <p>Plano atual: <b>{e(plan['label'])}</b> · <b>{plan['price']}€/mês</b> ·
       alojamentos: <b>{limit_txt}</b>{sub_txt}.</p>
    {manage}
    <div class="cards">{plan_cards}</div>
    {foot}"""
    return page("Faturação", body)


@app.route("/billing/request", methods=["POST"], endpoint="request_plan_route")
@login_required
def request_plan_route():
    db.request_plan(_host_id(), request.form.get("plan", ""))
    return redirect(url_for("billing"))


@app.route("/billing/checkout", methods=["POST"])
@login_required
def billing_checkout():
    import billing as bl
    host = auth.current_host()
    plan = request.form.get("plan", "")
    base = os.environ.get("APP_BASE_URL", request.url_root.rstrip("/"))
    try:
        url = bl.checkout_url(host, plan,
                              f"{base}{url_for('billing')}?ok=1",
                              f"{base}{url_for('billing')}")
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("stripe checkout falhou: %s", exc)
        url = None
    return redirect(url or url_for("billing"))


@app.route("/billing/portal", methods=["POST"])
@login_required
def billing_portal():
    import billing as bl
    host = auth.current_host()
    base = os.environ.get("APP_BASE_URL", request.url_root.rstrip("/"))
    try:
        url = bl.portal_url(host, f"{base}{url_for('billing')}")
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("stripe portal falhou: %s", exc)
        url = None
    return redirect(url or url_for("billing"))


@app.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    import billing as bl
    if not bl.enabled():
        return "", 200
    try:
        kind = bl.handle_event(request.get_data(), request.headers.get("Stripe-Signature", ""))
        app.logger.info("stripe evento: %s", kind)
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("stripe webhook rejeitado: %s", exc)
        return "", 400
    return "", 200


@app.route("/inbox")
@login_required
def inbox():
    threads: dict[tuple, list] = defaultdict(list)
    for m in db.list_messages(_host_id()):
        threads[(m["unit"], m["guest"])].append(dict(m))

    if not threads:
        return page("Mensagens", "<h2>Mensagens</h2><p class='muted'>Ainda sem conversas.</p>")

    blocks = ""
    for (unit, guest), msgs in sorted(threads.items(),
                                      key=lambda kv: kv[1][-1]["created_at"], reverse=True):
        turns = ""
        for m in msgs:
            tag = (f" <a href='{url_for('tickets')}'>#T-{m['ticket_id']:04d}</a>"
                   if m.get("ticket_id") else "")
            kind_lbl = KIND_LABELS.get(m["kind"], m["kind"])
            if m["kind"] == "proactive":
                turns += (f"<tr><td class='muted'>{ago(m['created_at'])}</td>"
                          f"<td><span class='muted'>🤖 {e(m['reply'])}</span> "
                          f"<span class='k-proactive'>({e(kind_lbl)})</span></td></tr>")
            elif m["kind"] == "held":
                turns += (f"<tr><td class='muted'>{ago(m['created_at'])}</td>"
                          f"<td><b>{e(guest)}:</b> {e(m['body'])}<br>"
                          f"<span class='k-held'>⏳ resposta a aguardar aprovação — "
                          f"<a href='{url_for('review')}'>rever</a></span></td></tr>")
            else:
                turns += (f"<tr><td class='muted'>{ago(m['created_at'])}</td>"
                          f"<td><b>{e(guest)}:</b> {e(m['body'])}<br>"
                          f"<span class='muted'>🤖 {e(m['reply'])}</span>"
                          f" <span class='k-{m['kind']}'>({e(kind_lbl)}{tag})</span></td></tr>")
        blocks += (f"<h3>{e(unit)} · {e(guest)} <span class='pill'>{len(msgs)} msg</span></h3>"
                   f"<table>{turns}</table>")
    return page("Mensagens", f"<h2>Mensagens</h2>{blocks}")


# --------------------------------------------------------------------------- #
# Fila de revisão (feat_review) — a IA redige, o anfitrião aprova
# --------------------------------------------------------------------------- #

@app.route("/review")
@login_required
def review():
    host = auth.current_host()
    if not host["feat_review"]:
        return page("A rever",
                    "<h2>A rever</h2><p class='muted'>A revisão de respostas está "
                    "desligada para esta conta. Um administrador pode ativá-la.</p>")
    items = db.list_review_items(host["id"], pending_only=True)
    if not items:
        return page("A rever", "<h2>A rever</h2><p class='muted'>Sem respostas por "
                    "aprovar. As novas mensagens dos hóspedes aparecem aqui primeiro.</p>")

    blocks = ""
    for r in items:
        prop = "" if r["proposed_kind"] != "ticket" else (
            f"<p class='muted'>A IA propõe abrir um <b>pedido de manutenção</b> — "
            f"{e(db.CATEGORY_LABELS.get(r['category'], r['category']))}, prioridade "
            f"{e(db.PRIORITY_LABELS.get(r['priority'], r['priority']))}.</p>")
        ticket_check = "" if r["proposed_kind"] != "ticket" else (
            "<label class='toggle-row'><input type='checkbox' name='make_ticket' checked> "
            "Criar o pedido de manutenção ao aprovar</label>")
        blocks += f"""<div class="card" style="margin-bottom:16px">
          <div class="muted">{e(r['unit'])} · {e(r['guest'])} · {ago(r['created_at'])}</div>
          <p><b>Hóspede:</b> {e(r['guest_message'])}</p>
          {prop}
          <form method="post" action="{url_for('review_approve', item_id=r['id'])}">
            <div class="field"><label>Resposta a enviar (pode editar)</label>
              <textarea name="reply" style="min-height:90px">{e(r['draft_reply'])}</textarea></div>
            {ticket_check}
            <button type="submit">Aprovar e enviar</button>
            <button type="submit" formaction="{url_for('review_discard', item_id=r['id'])}"
                    class="ghost">Descartar</button>
          </form>
        </div>"""
    return page("A rever", f"<h2>A rever <span class='pill'>{len(items)}</span></h2>{blocks}")


@app.route("/review/<int:item_id>/approve", methods=["POST"])
@login_required
def review_approve(item_id: int):
    host = auth.current_host()
    item = db.get_review_item(item_id, host["id"])
    if item and item["status"] == "pending":
        import manager
        manager.apply_review(
            item,
            edited_reply=request.form.get("reply", "").strip() or item["draft_reply"],
            make_ticket=request.form.get("make_ticket") is not None,
            dispatch_enabled=bool(host["feat_dispatch"]),
        )
    return redirect(url_for("review"))


@app.route("/review/<int:item_id>/discard", methods=["POST"])
@login_required
def review_discard(item_id: int):
    host = auth.current_host()
    item = db.get_review_item(item_id, host["id"])
    if item and item["status"] == "pending":
        db.resolve_review_item(item_id, "discarded")
    return redirect(url_for("review"))


# --------------------------------------------------------------------------- #
# Workers
# --------------------------------------------------------------------------- #

TRADES = list(db.TICKET_CATEGORIES.keys())


def _worker_form(action: str, w: dict, heading: str, extra: str = "") -> str:
    picked = set((w.get("trades") or "general").split(","))
    trade_boxes = "".join(
        f"""<label class="toggle-row"><input type="checkbox" name="trades" value="{t}"
            {'checked' if t in picked else ''}> {db.CATEGORY_LABELS.get(t, t)}</label>"""
        for t in TRADES
    )
    active_box = ("" if not w else
        f"""<label class="toggle-row"><input type="checkbox" name="active"
            {'checked' if w.get('active', 1) else ''}> Ativo (recebe novos trabalhos)</label>""")
    return f"""<h2>{e(heading)}</h2>{extra}
    <form method="post" action="{action}" style="max-width:420px">
      <div class="field"><label>Nome</label>
        <input name="name" value="{e(w.get('name',''))}" required></div>
      <div class="field"><label>Número de WhatsApp (com indicativo, ex.: +351912000111)</label>
        <input name="phone" value="{e(w.get('phone',''))}" required></div>
      <div class="field"><label>Especialidades que este prestador cobre</label>{trade_boxes}</div>
      {active_box}
      <button type="submit">Guardar</button>
      <a href="{url_for('workers')}"><button type="button" class="ghost">Cancelar</button></a>
    </form>"""


@app.route("/workers")
@login_required
def workers():
    def _trades_pt(csv: str) -> str:
        return ", ".join(db.CATEGORY_LABELS.get(t.strip(), t.strip()) for t in csv.split(","))
    rows = "".join(
        f"<tr><td>{e(w['name'])}</td><td>{e(w['phone'])}</td><td>{e(_trades_pt(w['trades']))}</td>"
        f"<td>{'ativo' if w['active'] else 'inativo'}</td>"
        f"<td><a href='{url_for('edit_worker', worker_id=w['id'])}'>Editar</a></td></tr>"
        for w in db.list_workers(_host_id())
    ) or "<tr><td colspan='5' class='muted'>Ainda sem prestadores.</td></tr>"
    body = f"""<div class="toolbar"><h2>Prestadores</h2>
      <a href="{url_for('new_worker')}"><button>Adicionar prestador</button></a></div>
    <table><tr><th>Nome</th><th>WhatsApp</th><th>Especialidades</th><th>Estado</th><th></th></tr>{rows}</table>
    <p class="muted">Quando um hóspede reporta um problema, o pedido é atribuído
    automaticamente a um prestador ativo com a especialidade correspondente, que
    recebe uma mensagem de WhatsApp. O prestador responde <b>a caminho</b> /
    <b>concluído</b> para atualizar o pedido. (Requer a funcionalidade
    <i>Envio automático</i> — ativa por omissão.)</p>"""
    return page("Prestadores", body)


@app.route("/workers/new", methods=["GET", "POST"])
@login_required
def new_worker():
    err = ""
    form = {"trades": "general"}
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        trades = request.form.getlist("trades") or ["general"]
        form = {"name": name, "phone": phone, "trades": ",".join(trades)}
        if not name or not phone:
            err = "O nome e o número de WhatsApp são obrigatórios."
        else:
            db.create_worker(_host_id(), name, phone, trades)
            return redirect(url_for("workers"))
    extra = f"<div class='err'>{e(err)}</div>" if err else ""
    return page("Novo prestador", _worker_form(url_for("new_worker"), form, "Adicionar prestador", extra))


@app.route("/workers/<int:worker_id>", methods=["GET", "POST"])
@login_required
def edit_worker(worker_id: int):
    w = db.get_worker(worker_id, _host_id())
    if w is None:
        return page("Não encontrado", "<h2>Prestador não encontrado</h2>"), 404
    if request.method == "POST":
        if request.form.get("_action") == "delete":
            db.delete_worker(worker_id, _host_id())
            return redirect(url_for("workers"))
        db.update_worker(
            worker_id, _host_id(),
            request.form.get("name", ""), request.form.get("phone", ""),
            request.form.getlist("trades") or ["general"],
            request.form.get("active") is not None,
        )
        return redirect(url_for("workers"))
    delete_form = f"""<form method="post" style="margin-top:20px"
        onsubmit="return confirm('Remover {e(w['name'])}?')">
      <input type="hidden" name="_action" value="delete">
      <button type="submit" class="danger">Remover prestador</button></form>"""
    return page(f"Editar {w['name']}",
                _worker_form(url_for("edit_worker", worker_id=worker_id), dict(w),
                             f"Editar {w['name']}") + delete_form)


# --------------------------------------------------------------------------- #
# Admin — client management console
# --------------------------------------------------------------------------- #

@app.route("/admin")
@auth.admin_required
def admin_home():
    hosts = db.list_hosts_with_stats()
    totals = {
        "clients": len(hosts),
        "active": sum(1 for h in hosts if h["status"] == "active"),
        "properties": sum(h["n_properties"] for h in hosts),
        "open_tickets": sum(h["n_open_tickets"] for h in hosts),
    }
    cards = f"""<div class="cards">
      <div class="card"><div class="n">{totals['clients']}</div><div class="l">Clientes</div></div>
      <div class="card"><div class="n">{totals['active']}</div><div class="l">Ativos</div></div>
      <div class="card"><div class="n">{totals['properties']}</div><div class="l">Alojamentos</div></div>
      <div class="card"><div class="n">{totals['open_tickets']}</div><div class="l">Pedidos em aberto</div></div>
    </div>"""

    requests_html = ""
    pend = [h for h in hosts if h["requested_plan"] and h["requested_plan"] != h["plan"]]
    if pend:
        items = "".join(
            f"<li><a href='{url_for('admin_host', host_id=h['id'])}'>{e(h['email'])}</a> "
            f"pediu <b>{e(db.PLANS[h['requested_plan']]['label'])}</b> "
            f"(atual: {e(db.PLANS[h['plan']]['label'])})</li>" for h in pend
        )
        requests_html = f"<div class='warn'><b>Pedidos de plano pendentes</b><ul>{items}</ul></div>"

    rows = ""
    for h in hosts:
        st = e(h["status"])
        st_lbl = "ativo" if st == "active" else "suspenso"
        req = (f" <span class='pill'>pediu {e(db.PLANS[h['requested_plan']]['label'])}</span>"
               if h["requested_plan"] and h["requested_plan"] != h["plan"] else "")
        rows += f"""<tr>
          <td><a href="{url_for('admin_host', host_id=h['id'])}">{e(h['email'])}</a>
              <div class='muted'>{e(h['name'] or '—')} · registou-se {ago(h['created_at'])}</div></td>
          <td>{e(db.PLANS[h['plan']]['label'])}{req}</td>
          <td class="status-{st}">{st_lbl}</td>
          <td>{h['n_properties']}</td>
          <td>{h['n_open_tickets']}</td>
          <td>{h['n_messages']}</td>
        </tr>"""
    rows = rows or "<tr><td colspan='6' class='muted'>Ainda sem contas de clientes.</td></tr>"

    acts = ""
    for a in db.recent_activity(20):
        acts += (f"<tr><td class='muted'>{ago(a['created_at'])}</td>"
                 f"<td>{e(a['host_email'])}</td><td>{e(a['unit'])}</td>"
                 f"<td class='row-msg'>{e(a['guest'])}: {e(a['body'])}</td>"
                 f"<td class='k-{a['kind']}'>{e(KIND_LABELS.get(a['kind'], a['kind']))}</td></tr>")
    acts = acts or "<tr><td colspan='5' class='muted'>Sem atividade.</td></tr>"

    body = f"""<div class="toolbar"><h2>Clientes</h2>
      <a href="{url_for('admin_new_client')}"><button>Adicionar cliente</button></a></div>
    {cards}{requests_html}
    <table><tr><th>Conta</th><th>Plano</th><th>Estado</th><th>Alojamentos</th>
      <th>Pedidos em aberto</th><th>Mensagens</th></tr>{rows}</table>
    <h3>Atividade recente (todos os clientes)</h3>
    <table><tr><th>Quando</th><th>Cliente</th><th>Unidade</th><th>Mensagem</th><th>Tratada</th></tr>
    {acts}</table>"""
    return page("Clientes", body)


@app.route("/admin/clients/new", methods=["GET", "POST"])
@auth.admin_required
def admin_new_client():
    err = ""
    email = name = ""
    if request.method == "POST":
        email = request.form.get("email", "").strip()
        name = request.form.get("name", "").strip()
        import re as _re
        if not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            err = "Introduza um email válido."
        elif db.get_host_by_email(email):
            err = "Já existe uma conta com esse email."
        else:
            import secrets as _secrets, mailer
            from werkzeug.security import generate_password_hash
            hid = db.create_host(email, generate_password_hash(_secrets.token_urlsafe(16)),
                                 name, email_verified=True)
            token = db.create_token(hid, "reset")
            base = os.environ.get("APP_BASE_URL", "http://localhost:5051").rstrip("/")
            mailer.send_welcome(email, name, f"{base}/reset/{token}")
            return redirect(url_for("admin_host", host_id=hid, created=1))

    err_html = f"<div class='err'>{e(err)}</div>" if err else ""
    body = f"""<h2>Adicionar cliente</h2>{err_html}
    <p class="muted">Cria a conta e envia-lhe um email para definir a palavra-passe.
    O email fica marcado como confirmado (você está a validá-lo).</p>
    <form method="post" style="max-width:380px">
      <div class="field"><label>Nome</label>
        <input name="name" value="{e(name)}"></div>
      <div class="field"><label>Email</label>
        <input name="email" type="email" value="{e(email)}" required></div>
      <button type="submit">Criar e convidar</button>
      <a href="{url_for('admin_home')}"><button type="button" class="ghost">Cancelar</button></a>
    </form>"""
    return page("Adicionar cliente", body)


@app.route("/admin/clients/<int:host_id>")
@auth.admin_required
def admin_host(host_id: int):
    h = db.get_host(host_id)
    if h is None or h["role"] != "host":
        return page("Não encontrado", "<h2>Cliente não encontrado</h2>"), 404

    props = db.list_properties(host_id)
    tickets = db.list_tickets(host_id)
    msgs = db.list_messages(host_id)
    open_tickets = [t for t in tickets if t["status"] != "done"]

    status_btn = ("Reativar conta" if h["status"] == "suspended" else "Suspender conta")
    status_cls = "" if h["status"] == "suspended" else "danger"

    plan_opts = "".join(
        f"<option value='{k}' {'selected' if h['plan'] == k else ''}>{v['label']}</option>"
        for k, v in db.PLANS.items()
    )

    feature_rows = ""
    for key, (label, _) in db.FEATURES.items():
        checked = "checked" if h[key] else ""
        feature_rows += f"""<label class="toggle-row">
          <input type="checkbox" name="{key}" {checked}
                 onchange="this.form.submit()"> {e(label)}</label>"""

    prop_rows = "".join(
        f"<tr><td>{e(p['unit'])}</td><td>{e(p['name'])}</td><td><code>{e(p['code'])}</code></td></tr>"
        for p in props
    ) or "<tr><td colspan='3' class='muted'>Sem alojamentos.</td></tr>"

    tick_rows = "".join(
        f"<tr><td>T-{t['id']:04d}</td><td>{e(t['unit'])}</td><td>{e(t['issue'])}</td>"
        f"<td><span class='badge {e(t['priority'])}'>{e(db.PRIORITY_LABELS.get(t['priority'], t['priority']))}</span></td>"
        f"<td>{e(db.STATUS_LABELS.get(t['status'], t['status']))}</td></tr>"
        for t in tickets[:20]
    ) or "<tr><td colspan='5' class='muted'>Sem pedidos.</td></tr>"

    an = db.host_analytics(host_id)
    maxn = max((n for _, n in an["tickets_by_category"]), default=1)
    cat_bars = "".join(
        f"<div style='margin:4px 0'>{e(db.CATEGORY_LABELS.get(c, c))} "
        f"<span class='muted'>({n})</span>"
        f"<div class='bar' style='width:{max(6, round(180*n/maxn))}px'></div></div>"
        for c, n in an["tickets_by_category"]
    ) or "<span class='muted'>Sem pedidos.</span>"

    req_banner = ""
    if request.args.get("created"):
        req_banner += ("<div class='ok'>Conta criada. Enviámos um email de "
                       "boas-vindas com o link para definir a palavra-passe.</div>")
    if h["requested_plan"] and h["requested_plan"] != h["plan"]:
        req_banner = f"""<div class="warn">Este cliente pediu o plano
          <b>{e(db.PLANS[h['requested_plan']]['label'])}</b>.
          <form method="post" action="{url_for('admin_host_update', host_id=host_id)}" class="inline">
            <input type="hidden" name="_action" value="apply_request">
            <button type="submit">Aplicar pedido</button>
          </form></div>"""

    body = f"""<div class="toolbar">
      <h2>{e(h['email'])}</h2>
      <a href="{url_for('admin_home')}"><button class="ghost">Voltar aos clientes</button></a>
    </div>
    <p class="muted">{e(h['name'] or '—')} · registou-se {ago(h['created_at'])} ·
      email {'confirmado' if h['email_verified'] else 'não confirmado'} ·
      <span class="status-{e(h['status'])}">{'ativo' if h['status'] == 'active' else 'suspenso'}</span>
      {f"· subscrição: <b>{e(h['subscription_status'])}</b>" if h['subscription_status'] else "· sem subscrição Stripe"}</p>
    {req_banner}

    <div class="cards">
      <div class="card"><div class="n">{len(props)}</div><div class="l">Alojamentos</div></div>
      <div class="card"><div class="n">{len(open_tickets)}</div><div class="l">Pedidos em aberto</div></div>
      <div class="card"><div class="n">{an['messages']}</div><div class="l">Mensagens (total)</div></div>
      <div class="card"><div class="n">{an['messages_7d']}</div><div class="l">Mensagens (7 dias)</div></div>
      <div class="card"><div class="n">{an['auto_rate']}%</div><div class="l">Respondidas por IA</div></div>
      <div class="card"><div class="n">{an['reservations']}</div><div class="l">Reservas</div></div>
    </div>

    <h3>Pedidos por categoria</h3>
    {cat_bars}

    <h3>Gestão da conta</h3>
    <form method="post" action="{url_for('admin_host_update', host_id=host_id)}"
          style="display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:12px">
      <input type="hidden" name="_action" value="status">
      <button type="submit" class="{status_cls}">{status_btn}</button>
    </form>
    <form method="post" action="{url_for('admin_host_update', host_id=host_id)}"
          style="display:flex; gap:12px; align-items:center; margin-bottom:16px">
      <input type="hidden" name="_action" value="plan">
      <label>Plano</label>
      <select name="plan" style="width:auto" onchange="this.form.submit()">{plan_opts}</select>
    </form>
    <h3 style="margin-bottom:6px">Funcionalidades</h3>
    <form method="post" action="{url_for('admin_host_update', host_id=host_id)}">
      <input type="hidden" name="_action" value="features">
      {feature_rows}
    </form>

    <h3>Alojamentos do cliente</h3>
    <table><tr><th>Unidade</th><th>Nome</th><th>Código do hóspede</th></tr>{prop_rows}</table>

    <h3>Pedidos do cliente</h3>
    <table><tr><th>ID</th><th>Unidade</th><th>Problema</th><th>Prioridade</th><th>Estado</th></tr>{tick_rows}</table>
    """
    return page(h["email"], body)


@app.route("/admin/clients/<int:host_id>/update", methods=["POST"])
@auth.admin_required
def admin_host_update(host_id: int):
    h = db.get_host(host_id)
    if h is None or h["role"] != "host":
        return "Não encontrado", 404
    action = request.form.get("_action")
    if action == "status":
        db.set_host_status(host_id, "active" if h["status"] == "suspended" else "suspended")
    elif action == "plan":
        db.set_host_plan(host_id, request.form.get("plan", ""))
    elif action == "apply_request":
        if h["requested_plan"]:
            db.set_host_plan(host_id, h["requested_plan"])
    elif action == "features":
        for key in db.FEATURES:
            db.set_host_feature(host_id, key, request.form.get(key) is not None)
    return redirect(url_for("admin_host", host_id=host_id))


if __name__ == "__main__":
    _debug = os.environ.get("FLASK_DEBUG", "1") != "0"
    # Reloader parent has WERKZEUG_RUN_MAIN unset; only start in the child (or
    # when there is no reloader) so the scheduler thread runs exactly once.
    if not _debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        _start_scheduler()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5051)), debug=_debug)
