"""
Subscrições com Stripe.

Configuração por variáveis de ambiente:

    STRIPE_SECRET_KEY          sk_test_... / sk_live_...
    STRIPE_WEBHOOK_SECRET      whsec_...  (do endpoint de webhook)
    STRIPE_PRICE_INICIAL       price_...  (10€/mês)
    STRIPE_PRICE_INTERMEDIO    price_...  (25€/mês)
    STRIPE_PRICE_PRO          price_...  (99€/mês)

Sem `STRIPE_SECRET_KEY`, o Stripe fica desligado e a página de Faturação usa o
fluxo antigo (o anfitrião pede um plano, o admin aplica).

Criar os produtos/preços no Stripe de uma vez:

    python billing.py setup
"""

from __future__ import annotations

import os
import sys

import db

_KEY = os.environ.get("STRIPE_SECRET_KEY", "").strip()
WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "").strip()

_PRICE_ENV = {
    "inicial": "STRIPE_PRICE_INICIAL",
    "intermedio": "STRIPE_PRICE_INTERMEDIO",
    "pro": "STRIPE_PRICE_PRO",
}


def enabled() -> bool:
    return bool(_KEY and all(os.environ.get(v) for v in _PRICE_ENV.values()))


def _stripe():
    import stripe
    stripe.api_key = _KEY
    return stripe


def _price_id(plan: str) -> str:
    return os.environ.get(_PRICE_ENV.get(plan, ""), "")


def plan_for_price(price_id: str) -> str | None:
    for plan, env in _PRICE_ENV.items():
        if os.environ.get(env) == price_id:
            return plan
    return None


# --------------------------------------------------------------------------- #
# Checkout + portal
# --------------------------------------------------------------------------- #

def ensure_customer(host) -> str:
    if host["stripe_customer_id"]:
        return host["stripe_customer_id"]
    cust = _stripe().Customer.create(
        email=host["email"], name=host["name"] or None,
        metadata={"host_id": str(host["id"])},
    )
    db.set_stripe_customer(host["id"], cust.id)
    return cust.id


def checkout_url(host, plan: str, success_url: str, cancel_url: str) -> str | None:
    price = _price_id(plan)
    if not price:
        return None
    session = _stripe().checkout.Session.create(
        mode="subscription",
        customer=ensure_customer(host),
        line_items=[{"price": price, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        allow_promotion_codes=True,
        client_reference_id=str(host["id"]),
    )
    return session.url


def portal_url(host, return_url: str) -> str | None:
    if not host["stripe_customer_id"]:
        return None
    session = _stripe().billing_portal.Session.create(
        customer=host["stripe_customer_id"], return_url=return_url,
    )
    return session.url


# --------------------------------------------------------------------------- #
# Webhook
# --------------------------------------------------------------------------- #

# Estados do Stripe que valem como "a pagar / ativo".
_LIVE = {"active", "trialing", "past_due"}


def _sync_subscription(sub) -> None:
    """Atualiza o anfitrião a partir de um objeto Subscription do Stripe."""
    host = db.get_host_by_stripe_customer(sub["customer"])
    if not host:
        return
    price_id = sub["items"]["data"][0]["price"]["id"] if sub["items"]["data"] else ""
    plan = plan_for_price(price_id)
    status = sub["status"]
    if status in _LIVE and plan:
        db.set_subscription(host["id"], sub["id"], status, plan)
    else:  # canceled / unpaid / incomplete_expired -> volta ao plano base
        db.set_subscription(host["id"], "", "canceled", db.DEFAULT_PLAN)


def handle_event(payload: bytes, sig_header: str) -> str:
    stripe = _stripe()
    event = stripe.Webhook.construct_event(payload, sig_header, WEBHOOK_SECRET)
    t = event["type"]
    obj = event["data"]["object"]

    if t == "checkout.session.completed" and obj.get("mode") == "subscription":
        _sync_subscription(stripe.Subscription.retrieve(obj["subscription"]))
    elif t in ("customer.subscription.created", "customer.subscription.updated",
               "customer.subscription.deleted"):
        _sync_subscription(obj)
    elif t == "invoice.payment_failed":
        host = db.get_host_by_stripe_customer(obj["customer"])
        if host:
            db.set_subscription(host["id"], host["stripe_subscription_id"] or "",
                                "past_due", host["plan"])
    return t


# --------------------------------------------------------------------------- #
# Setup CLI
# --------------------------------------------------------------------------- #

def _setup() -> None:
    stripe = _stripe()
    print("A criar produtos/preços no Stripe...\n")
    for plan, meta in db.PLANS.items():
        product = stripe.Product.create(name=f"Gestor de Alojamento — {meta['label']}")
        price = stripe.Price.create(
            product=product.id, currency="eur", unit_amount=meta["price"] * 100,
            recurring={"interval": "month"},
        )
        print(f"{_PRICE_ENV[plan]}={price.id}")
    print("\nCopie estas linhas para o .env / variáveis do serviço.")


if __name__ == "__main__":
    if not _KEY:
        sys.exit("Defina STRIPE_SECRET_KEY primeiro.")
    if len(sys.argv) == 2 and sys.argv[1] == "setup":
        _setup()
    else:
        sys.exit("uso: python billing.py setup")
