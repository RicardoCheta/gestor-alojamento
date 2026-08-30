"""
Core logic: take an incoming guest message for a given property and either
  (a) answer it automatically from the property's information, or
  (b) open a maintenance ticket.

Two backends:
  - rule-based (default): keyword matching, runs offline
  - llm: uses Claude for classification + natural replies (use_llm=True)

`respond()` is the public entry point. It persists the ticket + message log
through db.py, so callers just send `result.reply` back to the guest.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

import db

# Topics the AI can answer directly, mapped to keywords (PT + EN) that trigger them.
FAQ_TOPICS = {
    "wifi": ["wifi", "wi-fi", "internet", "palavra-passe", "palavra passe", "senha",
             "password", "rede", "network"],
    "ac_instructions": ["ar condicionado", "ac", "a/c", "climatização", "aquecimento",
                        "aquecedor", "frio", "calor", "temperatura",
                        "air con", "aircon", "air conditioning", "heating", "heater"],
    "parking": ["estacionar", "estacionamento", "garagem", "parque", "carro",
                "park", "parking", "garage", "car"],
    "checkin": ["check in", "check-in", "checkin", "chegada", "chegar", "entrada",
                "cofre", "chave", "arrive", "arrival", "lockbox", "key"],
    "checkout": ["check out", "check-out", "checkout", "saída", "saida", "sair",
                 "partida", "late checkout", "leave"],
    "trash": ["lixo", "reciclagem", "reciclar", "caixote", "contentor",
              "trash", "garbage", "rubbish", "bin", "recycle"],
    "house_rules": ["regras", "festa", "festas", "fumar", "fumo", "barulho", "ruído",
                    "silêncio", "animais", "animal", "rules", "party", "smoking", "pets"],
    "address": ["morada", "endereço", "direções", "direcções", "como chego", "onde fica",
                "address", "directions"],
}

# Words that signal something is broken -> ticket, not an FAQ answer.
PROBLEM_SIGNALS = [
    # Portuguese
    "não funciona", "nao funciona", "não trabalha", "não está a funcionar",
    "deixou de funcionar", "deixou de trabalhar", "parou de funcionar", "não dá",
    "não pega", "não arranca", "avaria", "avariado", "avariada", "avariou",
    "partido", "partida", "partiu", "estragado", "estragada", "não liga", "nao liga",
    "não acende", "nao acende", "não aquece", "não arrefece", "fuga", "a pingar",
    "a verter", "inundação", "inundado", "entupido", "entupida", "entupiu",
    "sem água", "sem agua", "sem água quente",
    "sem luz", "sem eletricidade", "sem electricidade", "sem aquecimento",
    "parou", "preso", "presa", "cheiro", "cheira mal", "barata", "baratas",
    "insetos", "insectos", "bolor", "mofo", "sujo", "suja", "nódoa", "manchado",
    "danificado", "não fecha", "não abre",
    "lâmpada", "lampada", "fundida", "fundiu", "acabou", "não há", "nao ha", "falta",
    "trancado", "fechado fora", "não consigo entrar", "nao consigo entrar",
    # English
    "not working", "isn't working", "doesn't work", "won't", "broken", "broke",
    "isnt working", "doesnt work", "wont", "wont turn", "cant", "not turning on",
    "leak", "leaking", "flood", "clogged", "blocked", "no hot water", "no water",
    "no power", "no electricity", "won't turn on", "stopped", "stuck", "smell",
    "cockroach", "bugs", "mold", "mould", "dirty", "stain", "noise from", "damaged",
    "light bulb", "lightbulb", "bulb", "burnt out", "burned out", "is out",
    "ran out of", "out of toilet paper", "missing",
]

HIGH_PRIORITY = ["sem água", "sem agua", "sem luz", "sem eletricidade", "sem electricidade",
                 "inundação", "inundado", "gás", "gas", "fumo", "incêndio", "fogo",
                 "trancado", "não consigo entrar", "nao consigo entrar", "sem aquecimento",
                 "sem água quente", "sem agua quente", "não há água quente",
                 "nao ha agua quente", "não tem água quente",
                 "no water", "no power", "no electricity", "flood", "smoke", "fire",
                 "locked out", "can't get in", "no heat", "no hot water"]
LOW_PRIORITY = ["lâmpada", "lampada", "comando", "pilhas", "wifi lento", "internet lenta",
                "gaveta", "cabide", "gancho",
                "light bulb", "lightbulb", "bulb", "remote", "batteries", "slow wifi",
                "drawer", "hook", "hanger"]


@dataclass
class Result:
    kind: str            # "answer" | "ticket"
    property_id: int
    unit: str
    guest_message: str
    reply: str
    topic: str | None = None
    ticket: dict | None = None


def _row_get(prop, key: str) -> str:
    """Works with sqlite3.Row or plain dict."""
    try:
        return prop[key] or ""
    except (KeyError, IndexError):
        return ""


# --------------------------------------------------------------------------- #
# Rule-based backend
# --------------------------------------------------------------------------- #

def _has_kw(text: str, kw: str) -> bool:
    """Whole-word match, tolerating a plural 's'/'es' — avoids Portuguese
    collisions like 'ac' in 'estacionar' while still catching 'lâmpadas'."""
    return re.search(rf"(?<!\w){re.escape(kw)}(?:es|s)?(?!\w)", text) is not None


def _any_kw(text: str, keywords) -> bool:
    return any(_has_kw(text, kw) for kw in keywords)


def _classify_rule_based(message: str) -> tuple[str, str | None]:
    text = message.lower()
    is_problem = _any_kw(text, PROBLEM_SIGNALS)

    matched_topic = None
    for topic, keywords in FAQ_TOPICS.items():
        if _any_kw(text, keywords):
            matched_topic = topic
            break

    if is_problem and matched_topic != "wifi":
        return "ticket", matched_topic
    if matched_topic:
        return "answer", matched_topic
    if is_problem:
        return "ticket", None
    return "answer", None


def _answer_from_kb(prop, topic: str | None) -> str:
    if topic == "wifi":
        net, pw = _row_get(prop, "wifi_network"), _row_get(prop, "wifi_password")
        notes = _row_get(prop, "wifi_notes")
        if not net and not pw:
            return ("Ainda não tenho os dados do WiFi — o anfitrião confirma em breve.")
        extra = f" {notes}" if notes else ""
        return f'A rede WiFi é "{net}" e a palavra-passe é {pw}.{extra}'
    if topic in ("ac_instructions", "parking", "checkin", "checkout", "trash", "house_rules"):
        val = _row_get(prop, topic)
        return val or ("Ainda não tenho essa informação — o anfitrião responde em breve.")
    if topic == "address":
        addr = _row_get(prop, "address")
        return f"A morada é {addr}." if addr else "O anfitrião envia-lhe a morada em breve."
    return ("Olá! Posso ajudar com WiFi, ar condicionado/aquecimento, "
            "estacionamento, check-in/check-out, lixo e regras da casa — ou "
            "diga-me o que se passa e registo um pedido de manutenção.")


def _category_for(message: str) -> str:
    text = message.lower()
    best, best_hits = "general", 0
    for cat, keywords in db.TICKET_CATEGORIES.items():
        hits = sum(1 for kw in keywords if _has_kw(text, kw))
        if hits > best_hits:
            best, best_hits = cat, hits
    return best


def _priority_for(message: str) -> str:
    text = message.lower()
    if _any_kw(text, HIGH_PRIORITY):
        return "High"
    if _any_kw(text, LOW_PRIORITY):
        return "Low"
    return "Medium"


def _summarize_issue(message: str) -> str:
    cleaned = re.sub(r"\s+", " ", message).strip()
    return (cleaned[:117] + "...") if len(cleaned) > 120 else cleaned


# --------------------------------------------------------------------------- #
# LLM backend (optional)
# --------------------------------------------------------------------------- #

LLM_MODEL = "claude-sonnet-5"

_LLM_SYSTEM = """És o assistente de um gestor de alojamento local. Recebes a
mensagem de um hóspede mais um JSON com tudo o que se sabe sobre o alojamento.

Decide uma de:
- "answer": a pergunta pode ser totalmente respondida com a informação do alojamento.
- "ticket": o hóspede reporta algo avariado/sujo/inseguro, ou precisa de uma pessoa.

Responde APENAS com JSON minificado:
{"kind":"answer","reply":"<resposta simpática ao hóspede, em português de Portugal>"}
ou
{"kind":"ticket","issue":"<título curto do problema, em português>","priority":"Low|Medium|High","reply":"<resposta tranquilizadora ao hóspede, em português de Portugal>"}

Prioridade: High = segurança / sem água / sem luz / trancado fora / sem aquecimento.
Low = trivial (lâmpada, pilhas, coisa menor). Medium = tudo o resto. As respostas ao
hóspede devem ter no máximo 4 frases, tom caloroso e específico, sempre em português
de Portugal. Nunca inventes dados que não estejam na informação do alojamento.
Os valores de "priority" ficam sempre em inglês (Low/Medium/High)."""


def property_context(prop) -> str:
    fields = ["unit", *db.PROPERTY_FIELDS]
    return json.dumps({f: _row_get(prop, f) for f in fields}, indent=2, ensure_ascii=False)


def _classify_llm(prop, message: str) -> dict:
    import anthropic  # lazy import so offline mode needs no install

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    resp = client.messages.create(
        model=LLM_MODEL,
        max_tokens=400,
        system=_LLM_SYSTEM,
        messages=[{
            "role": "user",
            "content": f"Informação do alojamento:\n{property_context(prop)}\n\nMensagem do hóspede:\n{message}",
        }],
    )
    raw = resp.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?|```$", "", raw).strip()
    return json.loads(raw)


# Mensagem imediata ao hóspede quando a resposta fica em revisão.
HOLDING_MSG = ("Obrigado pela sua mensagem! O anfitrião vai rever e responder-lhe "
               "dentro de momentos.")

_TICKET_REPLY = ("Obrigado por avisar — registei o pedido para a nossa equipa de "
                 "manutenção e alguém entrará em contacto em breve. "
                 "Lamentamos o incómodo!")
_FORWARDED_REPLY = ("Obrigado por avisar — sinalizei isto ao anfitrião, que vai "
                    "tratar do assunto. Lamentamos o incómodo!")


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #

def _plan(prop, message: str, use_llm: bool, auto_tickets: bool) -> dict:
    """Pure classification → a plan dict. No persistence, no messaging."""
    if use_llm:
        data = _classify_llm(prop, message)
        kind, reply = data["kind"], data["reply"]
        issue = data.get("issue", _summarize_issue(message))
        priority = data.get("priority", "Medium")
        topic = None
    else:
        kind, topic = _classify_rule_based(message)
        if kind == "ticket":
            issue, priority, reply = _summarize_issue(message), _priority_for(message), _TICKET_REPLY
        else:
            issue, priority, reply = "", "Medium", _answer_from_kb(prop, topic)

    category = _category_for(message) if kind == "ticket" else "general"
    if kind == "ticket" and not auto_tickets:
        kind, reply = "forwarded", _FORWARDED_REPLY
    return {"kind": kind, "reply": reply, "topic": topic,
            "issue": issue, "priority": priority, "category": category}


def _apply(prop, plan: dict, message: str, guest_name: str, reply_text: str, *,
           dispatch_enabled: bool, make_ticket: bool, deliver_to: str = "") -> Result:
    """Persist the outcome. If `deliver_to` is a phone, send the reply by
    outbound WhatsApp (review-approve path); otherwise the caller delivers it."""
    pid, unit = prop["id"], _row_get(prop, "unit")
    kind = plan["kind"]
    reply = reply_text

    ticket_row = None
    if kind == "ticket" and make_ticket:
        ticket_row = db.create_ticket(pid, plan["issue"] or _summarize_issue(message),
                                      plan["priority"], message, guest_name,
                                      category=plan["category"])
        if dispatch_enabled:
            import dispatch
            worker = dispatch.auto_assign_and_notify(db.get_ticket(ticket_row["id"]), prop)
            if worker:
                reply = reply.rstrip("!. ") + f" — já avisámos {worker['name']}, que vai passar por lá."
    elif kind == "ticket":            # ticket proposto mas o anfitrião não o quis criar
        kind = "answer"

    ticket_dict = dict(ticket_row) if ticket_row else None
    if ticket_dict:
        ticket_dict["ref"] = f"T-{ticket_dict['id']:04d}"
        ticket_dict["unit"] = unit

    db.log_message(pid, guest_name, message, reply, kind, plan["topic"],
                   ticket_row["id"] if ticket_row else None)

    if deliver_to:
        from notifications import send_whatsapp
        send_whatsapp(deliver_to, reply)

    return Result(kind, pid, unit, message, reply, topic=plan["topic"], ticket=ticket_dict)


def respond(prop, message: str, guest_name: str = "Guest", *,
            use_llm: bool = False, ai_enabled: bool = True, auto_tickets: bool = True,
            dispatch_enabled: bool = False, review: bool = False,
            guest_phone: str = "") -> Result:
    """Classify + act. Feature flags come from the host (set by an admin).

    review=True: hold the draft in the review queue instead of replying/acting.
    """
    pid, unit = prop["id"], _row_get(prop, "unit")

    if not ai_enabled:
        reply = ("Obrigado pela sua mensagem — encaminhei-a para o anfitrião, "
                 "que lhe responde em breve.")
        db.log_message(pid, guest_name, message, reply, "forwarded", None, None)
        return Result("forwarded", pid, unit, message, reply)

    plan = _plan(prop, message, use_llm, auto_tickets)

    if review:
        db.create_review_item(pid, guest_name, guest_phone, message, plan)
        db.log_message(pid, guest_name, message, HOLDING_MSG, "held", plan["topic"], None)
        return Result("held", pid, unit, message, HOLDING_MSG, topic=plan["topic"])

    return _apply(prop, plan, message, guest_name, plan["reply"],
                  dispatch_enabled=dispatch_enabled, make_ticket=(plan["kind"] == "ticket"))


def apply_review(item, *, edited_reply: str, make_ticket: bool,
                 dispatch_enabled: bool) -> Result:
    """Approve a queued draft: persist ticket/message, send the reply to the guest."""
    prop = db.get_property(item["property_id"])
    plan = {"kind": item["proposed_kind"], "reply": edited_reply, "topic": item["topic"],
            "issue": item["issue"], "priority": item["priority"], "category": item["category"]}
    result = _apply(prop, plan, item["guest_message"], item["guest"] or "Hóspede",
                    edited_reply, dispatch_enabled=dispatch_enabled,
                    make_ticket=(make_ticket and item["proposed_kind"] == "ticket"),
                    deliver_to=item["guest_phone"])
    db.resolve_review_item(item["id"], "approved")
    return result


def format_ticket(ticket: dict) -> str:
    prio = db.PRIORITY_LABELS.get(ticket["priority"], ticket["priority"])
    return (
        "🔧 Pedido de manutenção\n"
        f"Pedido:      {ticket.get('ref', ticket['id'])}\n"
        f"Alojamento:  {ticket.get('unit', ticket['property_id'])}\n"
        f"Problema:    {ticket['issue']}\n"
        f"Prioridade:  {prio}"
    )
