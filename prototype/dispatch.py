"""
Envio de pedidos (dispatch): encaminha um novo pedido de manutenção para o
prestador certo por WhatsApp e converte as respostas do prestador em mudanças de
estado do pedido.

    hóspede reporta problema
        -> manager.respond() abre um pedido
        -> auto_assign_and_notify() escolhe um prestador pela especialidade e avisa-o
        -> o prestador responde "a caminho" / "concluído"
        -> handle_worker_reply() muda o pedido para em curso / concluído
"""

from __future__ import annotations

import db
from notifications import send_whatsapp

_ON_IT = ["a caminho", "vou já", "vou la", "vou lá", "a caminho.", "sim", "ok", "okay",
          "está bem", "esta bem", "aceito", "combinado", "já vou", "on it", "omw",
          "coming", "will do", "got it", "yes", "accept"]
_DONE = ["concluído", "concluido", "feito", "resolvido", "terminado", "acabei",
         "está feito", "esta feito", "pronto", "done", "fixed", "resolved",
         "complete", "completed", "finished"]


def _ticket_ref(ticket) -> str:
    return f"T-{ticket['id']:04d}"


def _job_message(ticket, prop, prefix: str) -> str:
    prio = db.PRIORITY_LABELS.get(ticket["priority"], ticket["priority"])
    cat = db.CATEGORY_LABELS.get(ticket["category"], ticket["category"])
    addr = prop["address"] or prop["unit"]
    return (
        f"🔧 {prefix} {_ticket_ref(ticket)} (prioridade {prio} · {cat})\n"
        f"Alojamento: {prop['unit']}{' — ' + prop['name'] if prop['name'] else ''}\n"
        f"Morada: {addr}\n"
        f"Problema: {ticket['issue']}\n\n"
        f'Responda "a caminho" para aceitar, ou "concluído" quando terminar.'
    )


def auto_assign_and_notify(ticket, prop):
    """Atribui `ticket` a um prestador e avisa-o por WhatsApp. Devolve o prestador ou None."""
    worker = db.pick_worker_for(prop["host_id"], ticket["category"])
    if not worker:
        return None
    db.assign_ticket(ticket["id"], worker["id"])
    send_whatsapp(worker["phone"], _job_message(ticket, prop, "Novo trabalho"))
    return worker


def notify_worker_manual(ticket, prop, worker) -> None:
    """Usado quando o anfitrião reatribui um pedido manualmente no painel."""
    send_whatsapp(worker["phone"], _job_message(ticket, prop, "Trabalho atribuído a si:"))


def handle_worker_reply(worker, text: str) -> str:
    """Interpreta uma mensagem recebida de um prestador. Devolve a resposta a enviar."""
    low = text.strip().lower()
    ticket = db.latest_open_ticket_for_worker(worker["id"])
    if not ticket:
        return "Obrigado — de momento não tem trabalhos em aberto."

    if any(k in low for k in _DONE):
        db.set_ticket_status_direct(ticket["id"], "done")
        return f"{_ticket_ref(ticket)} marcado como concluído. Obrigado!"
    if any(k in low for k in _ON_IT):
        db.set_ticket_status_direct(ticket["id"], "in progress")
        return f"Obrigado — {_ticket_ref(ticket)} marcado como em curso."
    return (f"Trabalho {_ticket_ref(ticket)}: {ticket['issue']}\n"
            f'Responda "a caminho" para aceitar ou "concluído" quando terminar.')
