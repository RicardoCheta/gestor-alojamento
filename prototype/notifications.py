"""
WhatsApp de saída (avisos a prestadores, respostas aprovadas, mensagens
proativas). Delega em `whatsapp.py`, que suporta Twilio e a Meta Cloud API —
ou modo simulado se nada estiver configurado.
"""

from __future__ import annotations

import whatsapp

# compatibilidade: alguns módulos/testes ainda leem daqui
sent_log = whatsapp.sent_log


def live() -> bool:
    return whatsapp.provider() != ""


def send_whatsapp(to: str, body: str) -> bool:
    return whatsapp.send(to, body)
