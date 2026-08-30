# Deploy (Render)

O mesmo código corre em **SQLite** localmente e em **Postgres** em produção —
decidido pela variável `DATABASE_URL`. Um único serviço web serve o painel
(`/`) e o webhook do WhatsApp (`/hooks/whatsapp`), via `wsgi.py`. Um cron job à
parte corre as mensagens proativas.

## 1. Pré-requisitos

- Repositório no GitHub (o `prototype/` é a raiz da aplicação).
- Conta em https://render.com (plano gratuito chega para o piloto).

## 2. Deploy com o blueprint

O ficheiro [`render.yaml`](../render.yaml) (na raiz do repositório) descreve tudo:
base de dados Postgres + serviço web + cron. A app em si vive em `prototype/`
(`rootDir` no blueprint).

1. No Render: **New → Blueprint**, aponta para o repositório.
2. O Render cria a base de dados, o serviço web e o cron.
3. Preenche as variáveis marcadas `sync: false` no painel de cada serviço:
   - `APP_BASE_URL` = `https://<nome-do-servico>.onrender.com`
   - `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_WHATSAPP_FROM`
   - **Email**: `SMTP_HOST`, `SMTP_USER`, `SMTP_PASSWORD`, `MAIL_FROM`
     (cria uma conta grátis em Brevo / Resend / Postmark e usa as credenciais
     SMTP; verifica o teu domínio de envio no painel do fornecedor para SPF/DKIM)
   - (opcional) `ANTHROPIC_API_KEY` e `USE_LLM=1`
   - `FLASK_SECRET_KEY` é gerado automaticamente.

   Testa o email a partir da shell do serviço:
   `python mailer.py o-teu-email@exemplo.com`
4. `DATABASE_URL` é ligado automaticamente à base de dados.

O esquema é criado/migrado no arranque (`db.init_db()` corre com um *advisory
lock* para vários processos poderem arrancar em paralelo sem conflito).

## 3. Ligar o Twilio

No sandbox (ou no número aprovado), define **"When a message comes in"**:

```
https://<nome-do-servico>.onrender.com/hooks/whatsapp
```

método **POST**. Com `TWILIO_AUTH_TOKEN` definido, o webhook rejeita (403)
qualquer pedido que não venha assinado pelo Twilio.

## 4. Criar a tua conta de admin

**Com shell** (planos pagos): `python create_admin.py o-teu-email 'password'`.

**Sem shell** (free tier): no serviço web → Environment, adiciona
```
BOOTSTRAP_ADMIN = o-teu-email@exemplo.com:uma-password-de-8+-caracteres
```
Save → no arranque seguinte a conta de admin é criada (só se ainda não existir
nenhuma). Entra em `/login`, confirma, e **remove a variável `BOOTSTRAP_ADMIN`**.

Para **dados de teste** (um anfitrião `demo@example.com` / `demo1234` com 2
alojamentos e códigos de hóspede, útil para testar o WhatsApp antes de ter
SMTP): adiciona `SEED_DEMO = 1`. Corre uma vez, é não-destrutivo. Remove depois.

## 5. Mensagens proativas

No plano gratuito, o scheduler corre **dentro do serviço web** (`RUN_SCHEDULER=1`
+ `--workers 1` no blueprint) — de 10 em 10 min verifica os check-ins de hoje e
os check-outs de amanhã.

Limitação do free tier: o serviço **adormece após 15 min sem tráfego** e o
scheduler pára enquanto dorme; ao primeiro pedido acorda e recupera o atraso
(check-in/out têm folga suficiente para isto não ser problema num piloto).

Para produção a sério: sobe o web para **Starter** (sempre ligado) ou cria um
serviço `worker`/`cron` dedicado a correr `python scheduler.py` e tira o
`RUN_SCHEDULER` do web (com >1 worker, o scheduler no web duplicaria envios).

## 6. Stripe (subscrições)

Opcional — sem `STRIPE_SECRET_KEY` a Faturação usa o fluxo "o anfitrião pede um
plano, o admin aplica".

1. Conta em https://stripe.com → **modo de teste** para começar.
2. `Developers → API keys` → copia a *Secret key* para `STRIPE_SECRET_KEY`.
3. Cria os preços (na shell do serviço, com a chave já definida):
   ```bash
   python billing.py setup
   ```
   Copia as 3 linhas `STRIPE_PRICE_*` que imprime para as variáveis do serviço.
4. `Developers → Webhooks → Add endpoint`:
   - URL: `https://<nome-do-servico>.onrender.com/stripe/webhook`
   - eventos: `checkout.session.completed`, `customer.subscription.created`,
     `customer.subscription.updated`, `customer.subscription.deleted`,
     `invoice.payment_failed`
   - copia o *Signing secret* para `STRIPE_WEBHOOK_SECRET`
5. Reinicia o serviço. Na página **Faturação** os anfitriões passam a ver
   **Subscrever** (checkout do Stripe) e **Gerir subscrição** (portal do cliente).

Testar localmente: `stripe listen --forward-to localhost:5051/stripe/webhook`
e usa o secret que o `stripe listen` imprime.

Passar a produção: troca as chaves `sk_test_…` / `whsec_…` / `price_…` pelas de
*live mode* e volta a correr o passo 3–4 no modo live.

## Notas

- **Free tier**: o serviço web adormece após inatividade (primeiro pedido
  demora ~30 s). Para um piloto a sério, sobe o web para *Starter* (~7 $/mês).
- **Backups**: o Postgres gratuito do Render não tem backups automáticos —
  liga-os ou exporta com `pg_dump` regularmente.
- **Railway / Fly.io**: também servem. Usa o `Procfile` e define `DATABASE_URL`
  para a base Postgres gerida por eles; corre o `scheduler.py` como cron/worker.
- **Correr Postgres localmente** (para testar o caminho de produção):
  ```bash
  export DATABASE_URL='postgresql://USER:PASS@localhost:5432/gestor'
  python demo.py           # ALLOW_DB_RESET=1 se quiseres que apague tudo
  gunicorn wsgi:app --bind 127.0.0.1:8000
  ```
