"""
Abstração do canal WhatsApp — suporta dois fornecedores:

  * **Twilio** (sandbox ou número próprio): resposta síncrona por TwiML.
      TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_WHATSAPP_FROM

  * **Meta WhatsApp Cloud API** (grátis até 1000 conversas/mês): webhook JSON,
    verificação por GET challenge, envio pela Graph API.
      WHATSAPP_PROVIDER=meta
      META_ACCESS_TOKEN        token de acesso (System User, permanente)
      META_PHONE_NUMBER_ID     da página "API Setup"
      META_VERIFY_TOKEN        string à tua escolha (igual à do painel Meta)
      META_APP_SECRET          App > Settings > Basic (para validar assinatura)
      META_API_VERSION         opcional, default v21.0

Sem nenhum configurado, corre em modo **simulado** (imprime na consola).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.request
from dataclasses import dataclass

# --- Twilio -----------------------------------------------------------------
_TW_SID = os.environ.get("TWILIO_ACCOUNT_SID", "").strip()
_TW_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "").strip()
_TW_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "").strip()

# --- Meta -----------------------------------------------------------------
_META_TOKEN = os.environ.get("META_ACCESS_TOKEN", "").strip()
_META_PHONE_ID = os.environ.get("META_PHONE_NUMBER_ID", "").strip()
_META_VERIFY = os.environ.get("META_VERIFY_TOKEN", "").strip()
_META_SECRET = os.environ.get("META_APP_SECRET", "").strip()
_META_VER = os.environ.get("META_API_VERSION", "v21.0").strip()

sent_log: list[dict] = []


def provider() -> str:
    forced = os.environ.get("WHATSAPP_PROVIDER", "").strip().lower()
    if forced in ("twilio", "meta"):
        return forced
    if _META_TOKEN and _META_PHONE_ID:
        return "meta"
    if _TW_SID and _TW_TOKEN and _TW_FROM:
        return "twilio"
    return ""


def canonical(phone: str) -> str:
    """-> '+351912345678'  (aceita 'whatsapp:+351…', '351…', '+351…')."""
    p = (phone or "").strip()
    if p.startswith("whatsapp:"):
        p = p[len("whatsapp:"):]
    digits = "".join(c for c in p if c.isdigit())
    return "+" + digits if digits else ""


# --------------------------------------------------------------------------- #
# Inbound
# --------------------------------------------------------------------------- #

@dataclass
class Inbound:
    from_: str      # canónico, '+351…'
    body: str
    name: str


def verify_challenge(args) -> str | None:
    """Verificação do webhook da Meta (pedido GET). None se não aplicável."""
    if args.get("hub.mode") == "subscribe" and args.get("hub.verify_token") == _META_VERIFY:
        return args.get("hub.challenge")
    return None


def verify_signature(request) -> bool:
    prov = provider()
    if prov == "twilio":
        if not _TW_TOKEN:
            return True  # sem token -> primeiro teste local
        from twilio.request_validator import RequestValidator
        sig = request.headers.get("X-Twilio-Signature", "")
        v = RequestValidator(_TW_TOKEN)
        urls = [request.url]
        override = os.environ.get("TWILIO_WEBHOOK_URL", "").strip()
        if override:
            urls.insert(0, override)
        return any(v.validate(u, request.form, sig) for u in urls)
    if prov == "meta":
        if not _META_SECRET:
            return True  # sem app secret -> primeiro teste
        header = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(
            _META_SECRET.encode(), request.get_data(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(header, expected)
    return True


def parse_inbound(request) -> list[Inbound]:
    if provider() == "meta":
        try:
            data = request.get_json(force=True, silent=True) or {}
        except Exception:  # noqa: BLE001
            return []
        out: list[Inbound] = []
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                names = {c.get("wa_id"): c.get("profile", {}).get("name", "Hóspede")
                         for c in value.get("contacts", [])}
                for m in value.get("messages", []):
                    if m.get("type") != "text":
                        continue
                    wa_id = m.get("from", "")
                    out.append(Inbound(canonical(wa_id),
                                       (m.get("text", {}) or {}).get("body", "").strip(),
                                       names.get(wa_id, "Hóspede")))
        return out

    # Twilio (ou modo simulado / local): POST form-encoded do Twilio.
    body = (request.form.get("Body") or "").strip()
    frm = canonical(request.form.get("From", ""))
    if not body or not frm:
        return []
    return [Inbound(frm, body, request.form.get("ProfileName", "Hóspede"))]


# --------------------------------------------------------------------------- #
# Outbound
# --------------------------------------------------------------------------- #

def send(to: str, body: str) -> bool:
    """Envia uma mensagem de texto. `to` em qualquer formato (é normalizado)."""
    dest = canonical(to)
    prov = provider()

    if not prov:
        print(f"\n[WhatsApp -> {dest}] (simulado)\n{body}\n", flush=True)
        sent_log.append({"to": dest, "body": body, "simulated": True})
        return True

    try:
        if prov == "twilio":
            from twilio.rest import Client
            Client(_TW_SID, _TW_TOKEN).messages.create(
                from_=_TW_FROM, to=f"whatsapp:{dest}", body=body)
        else:  # meta
            url = f"https://graph.facebook.com/{_META_VER}/{_META_PHONE_ID}/messages"
            payload = json.dumps({
                "messaging_product": "whatsapp",
                "to": dest.lstrip("+"),
                "type": "text",
                "text": {"body": body, "preview_url": False},
            }).encode()
            req = urllib.request.Request(url, data=payload, method="POST", headers={
                "Authorization": f"Bearer {_META_TOKEN}",
                "Content-Type": "application/json",
            })
            with urllib.request.urlopen(req, timeout=15) as r:
                r.read()
        sent_log.append({"to": dest, "body": body, "simulated": False})
        return True
    except Exception as exc:  # noqa: BLE001 — protótipo: regista e continua
        detail = getattr(exc, "read", lambda: b"")()
        print(f"[WhatsApp falhou -> {dest}] {exc} {detail!r}", flush=True)
        return False
