"""
Webhook de WhatsApp do Gestor de Alojamento.

Recebe mensagens dos hóspedes (Twilio ou Meta Cloud API — ver `whatsapp.py`),
passa-as pelo `manager.respond` e responde.

Como o mesmo número serve todos os anfitriões, cada hóspede indica uma vez o
**código do hóspede** do alojamento (mostrado na página Alojamentos). A
associação telefone -> alojamento fica guardada na base de dados.

Local:
    python3 webhook.py        # http://localhost:5050/whatsapp
    ngrok http 5050           # e aponta o webhook do fornecedor para /whatsapp

Em produção corre dentro do serviço web (wsgi.py) em /hooks/whatsapp.
"""

from __future__ import annotations

import os
import re

from flask import Flask, Response, abort, request
from twilio.twiml.messaging_response import MessagingResponse
from werkzeug.middleware.proxy_fix import ProxyFix

import db
import whatsapp as wa
from manager import format_ticket, respond

app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

USE_LLM = os.environ.get("USE_LLM", "0") == "1"

db.init_db()


def _extract_code(text: str) -> str | None:
    """O código do hóspede tem 6 caracteres hex. Aceita-o sozinho ou numa frase."""
    for token in re.findall(r"[0-9a-fA-F]{6}", text):
        if db.get_property_by_code(token):
            return token.lower()
    return None


def handle_message(from_phone: str, body: str, profile_name: str) -> str | None:
    """Processa uma mensagem recebida e devolve o texto de resposta (ou None)."""
    # É um prestador a responder sobre um trabalho?
    worker = db.get_worker_by_phone(from_phone)
    if worker is not None:
        import dispatch
        return dispatch.handle_worker_reply(worker, body)

    prop = db.get_guest_property(from_phone)

    if prop is None:
        code = _extract_code(body)
        if code:
            linked = db.get_property_by_code(code)
            db.set_guest_property(from_phone, linked["id"])
            return (f"Obrigado! Está associado a {linked['unit']}"
                    f"{' — ' + linked['name'] if linked['name'] else ''}. "
                    f"Pergunte o que quiser — WiFi, ar condicionado, estacionamento, "
                    f"check-out — ou reporte um problema.")
        return ("Olá! Sou o assistente da sua estadia. Qual é o código do hóspede "
                "do seu alojamento? (6 caracteres, na mensagem de reserva do anfitrião)")

    # Permite re-associar com um novo código a meio da conversa.
    code = _extract_code(body)
    if code and len(body.strip()) <= 12:
        linked = db.get_property_by_code(code)
        if linked["id"] != prop["id"]:
            db.set_guest_property(from_phone, linked["id"])
            return f"Feito — agora associado a {linked['unit']}."

    host = db.get_host(prop["host_id"])
    if host is None or host["status"] == "suspended" or not host["feat_whatsapp"]:
        return ("O assistente não está ativo neste momento — por favor contacte "
                "diretamente o seu anfitrião.")

    result = respond(prop, body, guest_name=profile_name, use_llm=USE_LLM,
                     ai_enabled=bool(host["feat_ai"]),
                     auto_tickets=bool(host["feat_auto_tickets"]),
                     dispatch_enabled=bool(host["feat_dispatch"]),
                     review=bool(host["feat_review"]),
                     guest_phone=from_phone)
    if result.ticket:
        app.logger.info("Pedido criado:\n%s", format_ticket(result.ticket))
    if result.kind == "held":
        app.logger.info("Resposta em revisão para %s (%s)", prop["unit"], profile_name)
        _notify_host_review(host, prop["unit"], profile_name)
    return result.reply


@app.route("/whatsapp", methods=["GET", "POST"])
def whatsapp_webhook():
    # Verificação do webhook da Meta (pedido GET).
    if request.method == "GET":
        challenge = wa.verify_challenge(request.args)
        return (challenge, 200) if challenge is not None else ("", 403)

    if not wa.verify_signature(request):
        app.logger.warning("Assinatura de webhook inválida (%s).", wa.provider())
        abort(403)

    replies: list[tuple[str, str]] = []
    for msg in wa.parse_inbound(request):
        if not msg.body or not msg.from_:
            continue
        text = handle_message(msg.from_, msg.body, msg.name)
        if text:
            replies.append((msg.from_, text))

    if wa.provider() == "meta":
        # A Meta não aceita resposta inline — envia pela API e responde 200.
        for to, text in replies:
            wa.send(to, text)
        return ("", 200)

    # Twilio (ou local): resposta síncrona por TwiML.
    resp = MessagingResponse()
    for _, text in replies:
        resp.message(text)
    return Response(str(resp), mimetype="application/xml")


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
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5050)), debug=True)
