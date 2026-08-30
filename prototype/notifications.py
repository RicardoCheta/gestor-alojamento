"""
Outbound WhatsApp (host -> worker dispatch messages, and confirmations).

Sends via the Twilio REST API when these env vars are set:

    TWILIO_ACCOUNT_SID
    TWILIO_AUTH_TOKEN
    TWILIO_WHATSAPP_FROM      e.g. "whatsapp:+14155238886"  (the sandbox number)

Otherwise it runs in **simulate** mode: the message is printed to the server
console and recorded, so the whole dispatch flow is testable offline. In the
Twilio sandbox a worker must have joined the sandbox for a real send to reach
them.
"""

from __future__ import annotations

import os

_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")

# Last messages sent in simulate mode — handy for tests / a debug view.
sent_log: list[dict] = []


def live() -> bool:
    return bool(_SID and _TOKEN and _FROM)


def _normalise(to: str) -> str:
    to = to.strip()
    if to.startswith("whatsapp:"):
        return to
    if not to.startswith("+"):
        to = "+" + "".join(c for c in to if c.isdigit())
    return f"whatsapp:{to}"


def send_whatsapp(to: str, body: str) -> bool:
    """Return True if handed off to Twilio (or simulated) without error."""
    target = _normalise(to)
    if not live():
        print(f"\n[WhatsApp -> {target}] (simulado)\n{body}\n", flush=True)
        sent_log.append({"to": target, "body": body, "simulated": True})
        return True
    try:
        from twilio.rest import Client
        Client(_SID, _TOKEN).messages.create(from_=_FROM, to=target, body=body)
        sent_log.append({"to": target, "body": body, "simulated": False})
        return True
    except Exception as exc:  # noqa: BLE001 — prototype: log and carry on
        print(f"[WhatsApp send failed -> {target}] {exc}", flush=True)
        return False
