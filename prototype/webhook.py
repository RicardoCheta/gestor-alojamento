"""
WhatsApp webhook for the AI Airbnb Property Manager.

Receives inbound WhatsApp messages from Twilio, runs them through
`manager.respond`, and replies to the guest.

Because the Twilio sandbox uses one shared number for every host, each guest is
asked once for their apartment's **guest code** (shown on the host's Properties
page). The phone number -> property link is then remembered in the database.

Run:
    .venv/bin/pip install -r requirements.txt
    export USE_LLM=0                 # or 1 + ANTHROPIC_API_KEY for Claude replies
    python3 webhook.py               # http://localhost:5050/whatsapp

Then expose it:
    ngrok http 5050
and point the Twilio sandbox "When a message comes in" webhook at
    https://<your-ngrok-subdomain>.ngrok-free.app/whatsapp
"""

from __future__ import annotations

import os
import re

from flask import Flask, abort, request
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse
from werkzeug.middleware.proxy_fix import ProxyFix

import db
from manager import format_ticket, respond

app = Flask(__name__)
# ngrok / a hosting proxy terminates HTTPS in front of us. Trust its forwarded
# headers so request.url matches the public URL Twilio signed against.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

USE_LLM = os.environ.get("USE_LLM", "0") == "1"
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")

db.init_db()


@app.before_request
def verify_twilio_signature():
    """Reject anything that isn't a genuine, correctly-signed Twilio request.

    Skipped (with a warning) if TWILIO_AUTH_TOKEN isn't set, so first-run local
    testing still works — but set it before pointing a public URL at this.
    """
    if request.endpoint != "whatsapp":
        return None
    if not TWILIO_AUTH_TOKEN:
        app.logger.warning("TWILIO_AUTH_TOKEN not set — skipping signature check.")
        return None
    validator = RequestValidator(TWILIO_AUTH_TOKEN)
    signature = request.headers.get("X-Twilio-Signature", "")
    # Behind Render's proxy + the /hooks mount, request.url is the public https
    # URL. Also try an override so a mismatch is easy to fix without redeploy.
    candidates = [request.url]
    override = os.environ.get("TWILIO_WEBHOOK_URL", "").strip()
    if override:
        candidates.insert(0, override)
    if not any(validator.validate(u, request.form, signature) for u in candidates):
        app.logger.warning("Assinatura Twilio inválida. URL usado: %s "
                           "(define TWILIO_WEBHOOK_URL se o teu endpoint diferir)",
                           request.url)
        abort(403)
    return None


def _extract_code(text: str) -> str | None:
    """A guest code is 6 hex chars. Accept it alone or inside a short message."""
    for token in re.findall(r"[0-9a-fA-F]{6}", text):
        if db.get_property_by_code(token):
            return token.lower()
    return None


@app.route("/whatsapp", methods=["POST"])
def whatsapp() -> str:
    from_number = request.form.get("From", "")          # e.g. "whatsapp:+3519..."
    body = (request.form.get("Body") or "").strip()
    profile_name = request.form.get("ProfileName", "Hóspede")

    resp = MessagingResponse()

    # Is this a maintenance worker replying about a job?
    worker = db.get_worker_by_phone(from_number)
    if worker is not None:
        import dispatch
        resp.message(dispatch.handle_worker_reply(worker, body))
        return str(resp)

    prop = db.get_guest_property(from_number)

    # First contact (or guest not yet linked to an apartment).
    if prop is None:
        code = _extract_code(body)
        if code:
            linked = db.get_property_by_code(code)
            db.set_guest_property(from_number, linked["id"])
            resp.message(
                f"Obrigado! Está associado a {linked['unit']}"
                f"{' — ' + linked['name'] if linked['name'] else ''}. "
                f"Pergunte o que quiser — WiFi, ar condicionado, estacionamento, "
                f"check-out — ou reporte um problema."
            )
            return str(resp)
        resp.message(
            "Olá! Sou o assistente da sua estadia. Qual é o código do hóspede do "
            "seu alojamento? (6 caracteres, na mensagem de reserva do anfitrião)"
        )
        return str(resp)

    # Allow re-linking with a new code mid-conversation.
    code = _extract_code(body)
    if code and len(body.strip()) <= 12:
        linked = db.get_property_by_code(code)
        if linked["id"] != prop["id"]:
            db.set_guest_property(from_number, linked["id"])
            resp.message(f"Feito — agora associado a {linked['unit']}.")
            return str(resp)

    host = db.get_host(prop["host_id"])
    if host is None or host["status"] == "suspended" or not host["feat_whatsapp"]:
        resp.message("O assistente não está ativo neste momento — por favor "
                     "contacte diretamente o seu anfitrião.")
        return str(resp)

    result = respond(prop, body, guest_name=profile_name, use_llm=USE_LLM,
                     ai_enabled=bool(host["feat_ai"]),
                     auto_tickets=bool(host["feat_auto_tickets"]),
                     dispatch_enabled=bool(host["feat_dispatch"]),
                     review=bool(host["feat_review"]),
                     guest_phone=from_number)
    resp.message(result.reply)
    if result.ticket:
        app.logger.info("Pedido criado:\n%s", format_ticket(result.ticket))
    if result.kind == "held":
        app.logger.info("Resposta em revisão para %s (%s)", prop["unit"], profile_name)
        _notify_host_review(host, prop["unit"], profile_name)
    return str(resp)


def _notify_host_review(host, unit: str, guest: str) -> None:
    try:
        import mailer
        base = os.environ.get("APP_BASE_URL", "http://localhost:5051").rstrip("/")
        mailer.send_host_notification(
            host["email"], "resposta para rever",
            f"O hóspede {guest} ({unit}) enviou uma mensagem. A IA já preparou uma "
            f"resposta — reveja e aprove no painel.",
            "Rever agora", f"{base}/review")
    except Exception as exc:  # noqa: BLE001
        app.logger.warning("falha ao notificar anfitrião: %s", exc)


@app.route("/", methods=["GET"])
def health() -> str:
    return "Webhook do Gestor de Alojamento a funcionar. POST para /whatsapp"


if __name__ == "__main__":
    # 5050 rather than 5000: on macOS, port 5000 is taken by AirPlay Receiver.
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5050)), debug=True)
