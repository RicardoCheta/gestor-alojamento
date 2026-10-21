"""
Envio de emails transacionais (verificação de email, recuperação de palavra-passe).

Usa SMTP — funciona com qualquer fornecedor (Brevo, Resend, Postmark, Amazon
SES, Mailgun, Gmail…). Configuração por variáveis de ambiente:

    SMTP_HOST       ex.: smtp-relay.brevo.com
    SMTP_PORT       587 (STARTTLS) ou 465 (SSL)   — default 587
    SMTP_USER
    SMTP_PASSWORD
    MAIL_FROM       ex.: "Gestor de Alojamento <ola@teudominio.pt>"

Sem `SMTP_HOST` definido, os emails são apenas impressos na consola (bom para
desenvolvimento local e para os testes). Testar a configuração:

    python mailer.py o-teu-email@exemplo.com
"""

from __future__ import annotations

import os
import smtplib
import ssl
import sys
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

APP_NAME = "Gestor de Alojamento"

SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
MAIL_FROM = os.environ.get("MAIL_FROM", "").strip() or "no-reply@localhost"
# STARTTLS on by default (port 587). Set SMTP_STARTTLS=0 only for a local test
# server; port 465 uses implicit SSL and ignores this.
SMTP_STARTTLS = os.environ.get("SMTP_STARTTLS", "1").lower() not in ("0", "false", "no")


def configured() -> bool:
    return bool(SMTP_HOST and MAIL_FROM and "@" in MAIL_FROM)


def _console(to: str, subject: str, text: str) -> None:
    print(f"\n----- EMAIL (consola; SMTP_HOST não definido) -----\n"
          f"Para:     {to}\nAssunto:  {subject}\n\n{text}\n"
          f"--------------------------------------------------\n", flush=True)


def send(to: str, subject: str, text: str, html: str | None = None) -> bool:
    """Devolve True se o email foi entregue ao servidor SMTP (ou impresso na consola)."""
    if not configured():
        _console(to, subject, text)
        return True

    msg = EmailMessage()
    name, addr = parseaddr(MAIL_FROM)
    msg["From"] = formataddr((name or APP_NAME, addr))
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    try:
        ctx = ssl.create_default_context()
        if SMTP_PORT == 465:
            with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx, timeout=15) as s:
                if SMTP_USER:
                    s.login(SMTP_USER, SMTP_PASSWORD)
                s.send_message(msg)
        else:
            with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
                if SMTP_STARTTLS:
                    s.starttls(context=ctx)
                if SMTP_USER:
                    s.login(SMTP_USER, SMTP_PASSWORD)
                s.send_message(msg)
        return True
    except Exception as exc:  # noqa: BLE001
        # Não deixar o utilizador preso: regista o erro e mostra o link na consola.
        print(f"[mailer] falha ao enviar para {to}: {exc}", flush=True)
        _console(to, subject, text)
        return False


# --------------------------------------------------------------------------- #
# Modelos (português de Portugal)
# --------------------------------------------------------------------------- #

def _wrap_html(title: str, body_html: str, cta_label: str, cta_url: str) -> str:
    return f"""<div style="font-family:-apple-system,Segoe UI,Roboto,sans-serif;
      max-width:480px;margin:0 auto;color:#1c1e21;line-height:1.5">
      <h2 style="font-size:18px">{title}</h2>
      {body_html}
      <p style="margin:24px 0">
        <a href="{cta_url}" style="background:#1877f2;color:#fff;text-decoration:none;
           padding:11px 18px;border-radius:8px;display:inline-block">{cta_label}</a>
      </p>
      <p style="color:#606770;font-size:13px">Se o botão não funcionar, copie este link:<br>
        <span style="word-break:break-all">{cta_url}</span></p>
      <p style="color:#606770;font-size:13px">— {APP_NAME}</p>
    </div>"""


def send_verification(to: str, url: str) -> bool:
    subject = f"{APP_NAME}: confirme o seu email"
    text = (f"Olá,\n\nConfirme o seu email para começar a usar o {APP_NAME}:\n{url}\n\n"
            f"Se não criou esta conta, ignore esta mensagem.\n\n— {APP_NAME}")
    html = _wrap_html(
        "Confirme o seu email",
        f"<p>Falta um passo para começar a usar o {APP_NAME}.</p>",
        "Confirmar email", url)
    return send(to, subject, text, html)


def send_password_reset(to: str, url: str) -> bool:
    subject = f"{APP_NAME}: recuperar a palavra-passe"
    text = (f"Olá,\n\nRecebemos um pedido para redefinir a sua palavra-passe. "
            f"Abra este link (válido por tempo limitado):\n{url}\n\n"
            f"Se não foi você, ignore esta mensagem — a palavra-passe não muda.\n\n— {APP_NAME}")
    html = _wrap_html(
        "Recuperar a palavra-passe",
        "<p>Recebemos um pedido para redefinir a sua palavra-passe.</p>",
        "Definir nova palavra-passe", url)
    return send(to, subject, text, html)


def send_welcome(to: str, name: str, url: str) -> bool:
    """Convite a um anfitrião criado pelo operador — define a palavra-passe."""
    subject = f"Bem-vindo ao {APP_NAME}"
    text = (f"Olá {name or ''},\n\nFoi criada uma conta para si no {APP_NAME}. "
            f"Defina a sua palavra-passe para começar:\n{url}\n\n"
            f"Depois de entrar, adicione os seus alojamentos e a informação que o "
            f"assistente vai usar para responder aos hóspedes.\n\n— {APP_NAME}")
    html = _wrap_html(
        f"Bem-vindo ao {APP_NAME}",
        "<p>Foi criada uma conta para si. Defina a palavra-passe para começar — "
        "depois é só adicionar os seus alojamentos.</p>",
        "Definir palavra-passe", url)
    return send(to, subject, text, html)


def send_host_notification(to: str, subject: str, intro: str, cta_label: str, url: str) -> bool:
    text = f"{intro}\n\n{url}\n\n— {APP_NAME}"
    html = _wrap_html(subject, f"<p>{intro}</p>", cta_label, url)
    return send(to, f"{APP_NAME}: {subject}", text, html)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("uso: python mailer.py destinatario@exemplo.com")
    dest = sys.argv[1]
    print(f"Configurado: {configured()}  (SMTP_HOST={SMTP_HOST or '—'})")
    ok = send(dest, f"{APP_NAME}: teste de configuração",
              "Este é um email de teste. Se o recebeu, o SMTP está bem configurado.")
    print("Enviado." if ok else "Falhou (ver erro acima).")
