# Architecture — from prototype to product

## Prototype (this repo)

```
guest message ─▶ manager.handle_message(unit, text)
                     │
                     ├─ classify:  FAQ topic  or  problem
                     │
                     ├─ FAQ   ─▶ answer from properties.json ─▶ reply to guest
                     │
                     └─ problem ─▶ create ticket (tickets.json) ─▶ reassure guest
```

Classifier has two backends: offline keyword matching, or Claude
(`claude-sonnet-5`) for classification + natural-language replies.

## Production shape

### 1. Message intake
- **Airbnb**: no official inbound messaging API for hosts. Options: unified
  inbox providers (Hospitable, Hostaway, Guesty) that expose a messaging API and
  webhooks, or a channel manager. Start by integrating one of these rather than
  Airbnb directly.
- Also support **WhatsApp Business API** and SMS for direct bookings.
- Each inbound message arrives with a reservation/thread id → map to a `unit`.

### 2. Knowledge base per property
- **Done**: SQLite `properties` table, structured fields, dashboard editor, and
  `listing_import.py` — paste Airbnb text, Claude fills the fields (regex
  fallback offline).
- **Next**: a free-text "house manual" field, embedded for retrieval so the LLM
  can answer long-tail questions; move SQLite → Postgres.

### 3. AI reply engine
- **Prototype now**: keyword classifier (PT + EN, whole-word matching with
  plural tolerance) or Claude (`USE_LLM=1`) — both reply in pt-PT. Every message
  logged. `manager._plan()` is side-effect-free; `respond()` acts on it, or —
  when `feat_review` is on — parks the draft in a **review queue** so the host
  approves/edits before anything reaches the guest (`apply_review()`).
- Claude with the property context in the system prompt + retrieved manual chunks.
- **Next guardrails**: never share door codes before the check-in date; escalate
  refunds / cancellations / injuries / legal to a human; a confidence score that
  auto-holds low-confidence replies even when review is otherwise off.
- Log every exchange; let hosts review and correct (feeds back into the manual).

### 4. Ticketing + dispatch
- **Prototype now**: `tickets` carry a `category` (auto-classified) and an
  `assigned_worker_id`. `workers` table per host (name, WhatsApp, trades).
  `dispatch.py` picks a worker by trade on ticket creation, messages them via
  `notifications.py`, and `webhook.py` turns their `on it` / `done` replies into
  status changes. Gated by the `feat_dispatch` account flag.
- **Next**: photos on tickets (guest sends an image), SLA timers + escalation to
  the host if a worker doesn't acknowledge, per-property worker overrides,
  worker-facing view of their job list, cost tracking per ticket.

### 5. Dashboard, proactive messaging & billing
- **Prototype now** (all PT-PT): Resumo / Pedidos / Alojamentos / Reservas /
  Prestadores / Mensagens / Faturação / Conta. `reservations` table + two
  per-property templates (`tpl_precheckin`, `tpl_checkout`) drive `scheduler.py`,
  which `dashboard.py` runs on a 10-minute background thread (and can run by
  cron). Each proactive message sends once per reservation, logged as a
  `proactive` message. Gated by `feat_proactive`.
- **Billing**: `billing.py` — Stripe Checkout + Customer Portal + a signed
  `/stripe/webhook` that syncs `plan` / `subscription_status` from
  `customer.subscription.*` and `invoice.payment_failed`. Price-id ↔ plan-key
  mapping via env. With no `STRIPE_SECRET_KEY` it falls back to the
  request-a-plan / admin-applies flow (`requested_plan`).
- **Admin console**: create clients (welcome email), activity feed, per-client
  analytics (`db.host_analytics()`), plan / feature / status controls.
- **Next**: usage-metered add-ons, dunning emails, annual plans; richer analytics
  (response time, deflection trend), scheduled digests to hosts.

### 6. Auth, roles, data model & deploy
- **Storage**: `db.py` runs on **SQLite** locally and **Postgres** in production,
  picked from `DATABASE_URL`. One thin adapter (`_PgConn` translates `?`→`%s`,
  `_Row` gives dict rows that also take int indexes, `RETURNING id` for inserts);
  `init_db()` takes a Postgres advisory lock so web + cron can boot in parallel.
- **Deploy**: `wsgi.py` mounts the dashboard at `/` and the webhook at `/hooks`
  behind one `gunicorn` process; `render.yaml` provisions Postgres + web + a
  15-min cron running `scheduler.py`. `APP_BASE_URL` drives email links and the
  `Secure` cookie flag. See `docs/deploy.md`.
- **Schema**: multi-tenant —
  `hosts / properties / tickets / messages / reservations / workers /
  guest_sessions / auth_tokens`, every dashboard query filtered by `host_id`.
  `auth.py`
  does self-serve signup, Werkzeug password hashing, a signed session cookie, a
  5-try / 60-second brute-force brake, email verification and password reset.
  `mailer.py` sends those emails over SMTP (any provider — Brevo / Resend /
  Postmark / SES / Mailgun); with no SMTP configured the links go to the
  console. `webhook.py` verifies the Twilio request signature. A guest links their
  WhatsApp chat to a property with a 6-char per-property code.
- **Roles**: `host` and `admin`. Admins use a separate console (`/admin`) to
  manage client accounts — suspend/reactivate, set plan (property-count limit),
  toggle per-account features (`feat_ai`, `feat_whatsapp`, `feat_auto_tickets`)
  which `manager.py` / `webhook.py` honour at request time. Admins are created
  only via `create_admin.py`, never self-signup.
- **Next**: verify the sending domain (SPF/DKIM/DMARC in the email provider) for
  deliverability; welcome + ticket-notification emails. Add TOTP/WebAuthn — or
  delegate to an IdP (Auth0 / Clerk / WorkOS). More roles:
  co-host, cleaner (ticket-only view). Everything over HTTPS; session cookie
  `Secure` + `HttpOnly` + `SameSite`. Audit log of admin actions and ticket
  changes. Stripe for real plan billing. Per-host Twilio numbers so guests
  don't need a code.

## Key risks
- **Airbnb ToS / messaging access** — depends on going through an approved PMS.
- **Wrong automated answers** — mitigate with confidence gating + human review
  queue in the first weeks of each property.
- **Liability** on safety issues — always escalate, never let the AI close
  those autonomously.
