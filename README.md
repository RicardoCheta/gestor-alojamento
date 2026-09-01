# 🏡 Gestor de Alojamento com IA

Um assistente de IA que responde automaticamente às mensagens dos hóspedes com a
informação de cada alojamento e transforma problemas reais em pedidos de
manutenção. Interface e mensagens em **português de Portugal**.

## O que faz

**Responde automaticamente às perguntas comuns dos hóspedes** com os dados do
alojamento:

| O hóspede pergunta | A IA responde a partir de |
| --- | --- |
| "Onde está o WiFi?" | dados de `wifi` da unidade |
| "Como ligo o ar condicionado?" | `ac_instructions` |
| "Onde posso estacionar?" | `parking` |
| "Posso fazer check-out às 14h?" | política de `checkout` |

**Cria um pedido** quando o hóspede reporta um problema que não consegue só
responder:

```
🔧 Pedido de manutenção
Alojamento: 14B
Problema: Chuveiro a verter
Prioridade: Média
```

Os pedidos são encaminhados automaticamente para prestadores (limpeza / manutenção).

## Monetização

Subscrição mensal, sem plano gratuito:

| Plano | Alojamentos | Preço |
| --- | --- | --- |
| Inicial | 1 | 10€ / mês |
| Intermédio | até 5 | 25€ / mês |
| Pro | ilimitado | 99€ / mês |

- ~500 alojamentos (a ~5–10€/alojamento) → **3k€–5k€ / mês**

## Estrutura do repositório

```
ai-airbnb-property-manager/
├── README.md
├── render.yaml             # blueprint do Render (tem de estar na raiz)
├── prototype/
│   ├── db.py               # armazenamento — SQLite (local) ou Postgres (prod), via DATABASE_URL
│   ├── auth.py             # registo/login, hash de palavras-passe, verificação de email, recuperação, perfis
│   ├── manager.py          # núcleo: classificar → responder ou abrir pedido (+ categoria)
│   ├── dispatch.py         # encaminhar pedidos para prestadores, tratar as respostas por WhatsApp
│   ├── scheduler.py        # mensagens proativas (check-in / véspera de check-out)
│   ├── mailer.py           # emails transacionais por SMTP (verificação, recuperação)
│   ├── billing.py          # subscrições Stripe (checkout, portal, webhooks)
│   ├── notifications.py    # WhatsApp de saída (Twilio REST, ou simulado para a consola)
│   ├── listing_import.py   # colar um anúncio Airbnb → campos estruturados do alojamento
│   ├── dashboard.py        # app web: painel do anfitrião + consola de admin
│   ├── webhook.py          # servidor Flask que recebe mensagens do hóspede
│   ├── whatsapp.py         # canal WhatsApp — Twilio ou Meta Cloud API (ou simulado)
│   ├── wsgi.py             # entrypoint de produção (gunicorn): painel em / e webhook em /hooks
│   ├── create_admin.py     # CLI para criar uma conta de administrador da plataforma
│   ├── demo.py             # repõe a BD, cria contas demo, corre exemplos
│   ├── requirements.txt · Procfile · .env.example
│   └── data/
│       ├── properties.json  # dados de exemplo que o demo.py insere na BD
│       └── app.db           # base de dados SQLite local (criada no 1.º arranque, no .gitignore)
└── docs/
    ├── architecture.md
    ├── deploy.md            # deploy no Render (Postgres + web + cron)
    ├── whatsapp-meta.md     # ligar o WhatsApp via Meta Cloud API (grátis)
    ├── pilot.md             # runbook do piloto (operador)
    └── manual-anfitriao.md  # guia para o anfitrião piloto
```

## Correr o protótipo

```bash
cd prototype
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python demo.py
```

O `demo.py` cria `data/app.db` e semeia:

- um anfitrião **demo@example.com / demo1234** (plano Inicial) com dois
  apartamentos em Lisboa, prestadores, reservas, mensagens de exemplo, e corre
  as mensagens proativas devidas hoje
- um administrador da plataforma **admin@example.com / admin1234**

As contas de admin reais criadas com `create_admin.py` são mantidas entre execuções do `demo.py`.

> Sem `DATABASE_URL` a aplicação usa SQLite (`data/app.db`). Para testar o
> caminho de produção, define `DATABASE_URL=postgresql://...` — o mesmo código
> corre em Postgres. Deploy: [docs/deploy.md](docs/deploy.md).

Classificador por palavras-chave (offline) por omissão. Para respostas geradas pela Claude:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/pip install anthropic
.venv/bin/python demo.py --llm
```

## Painel do anfitrião

O [`prototype/dashboard.py`](prototype/dashboard.py) é a app web que os anfitriões
usam. A partir de `prototype/`:

```bash
.venv/bin/python dashboard.py          # http://localhost:5051
```

Qualquer pessoa pode **criar conta em `/signup`** — cada anfitrião só vê os seus
próprios alojamentos, pedidos e conversas (garantido em todas as queries de
[`db.py`](prototype/db.py)). Depois do `demo.py`, entre com `demo@example.com` /
`demo1234`.

Páginas:

- **Resumo** — mensagens hoje, % respondidas por IA, pedidos em aberto, atividade recente.
- **Pedidos** — a fila de manutenção, primeiro os abertos e depois por prioridade;
  mudar o estado ou o prestador atribuído em linha. Cada pedido é classificado
  automaticamente numa categoria (canalização / eletricidade / eletrodomésticos /
  limpeza / geral).
- **Alojamentos** — adicionar/editar/eliminar alojamentos e a informação que a IA
  usa para responder (WiFi, ar condicionado, estacionamento, check-in/out, lixo,
  regras). Cada alojamento tem um **código do hóspede** de 6 caracteres. Em
  *Adicionar alojamento* pode colar o texto do seu anúncio Airbnb e
  **✨ pré-preencher os campos** — via Claude quando `ANTHROPIC_API_KEY` está
  definido, senão um fallback simples com expressões regulares
  (`listing_import.py`). Reveja sempre antes de guardar.
- **A rever** *(só quando a funcionalidade está ligada)* — a IA redige a resposta
  mas não a envia; o anfitrião edita se quiser e clica **Aprovar e enviar** (ou
  **Descartar**). Entretanto o hóspede recebe "o anfitrião responde-lhe dentro de
  momentos". Ao aprovar, o pedido de manutenção é criado, o prestador é avisado
  e a resposta é enviada por WhatsApp. Bom para as primeiras semanas de cada
  alojamento novo — ver *Fila de revisão* abaixo.
- **Reservas** — datas de check-in/out e telefone do hóspede. Alimentam as
  *mensagens proativas* (ver abaixo).
- **Prestadores** — os seus prestadores de limpeza / manutenção, cada um com um
  número de WhatsApp e as especialidades que cobre. Ver *Envio de pedidos* abaixo.
- **Mensagens** — todas as conversas dos hóspedes, agrupadas por unidade + hóspede,
  mostrando o que a IA respondeu e que mensagens geraram um pedido.
- **Faturação** — plano atual, preço, alojamentos usados, e pedir mudança de plano
  (stub — sem pagamento real; ver abaixo).

### Fila de revisão

Com a funcionalidade **"Rever respostas antes de enviar"** ligada (um admin
ativa-a por conta — está desligada por omissão):

1. O hóspede envia uma mensagem. A IA classifica e **redige** a resposta (e o
   eventual pedido de manutenção) mas **não age**.
2. O hóspede recebe de imediato *"o anfitrião responde-lhe dentro de momentos"*.
3. Na aba **A rever** o anfitrião vê a mensagem, o rascunho (editável) e o que a
   IA propõe. Clica **Aprovar e enviar** ou **Descartar**.
4. Ao aprovar: o pedido é criado (se aplicável, com opção de não criar), o
   prestador é avisado, e a resposta editada é enviada ao hóspede por WhatsApp.

Recomendado nas primeiras semanas de cada alojamento novo, até se confiar nas
respostas automáticas. O `manager._plan()` faz a classificação sem efeitos
secundários; `manager.apply_review()` executa a decisão aprovada.

### Mensagens proativas (check-in / check-out)

Cada alojamento tem dois modelos editáveis (secção *Mensagens automáticas* no
formulário do alojamento), com variáveis como `{hospede}`, `{morada}`,
`{entrada}`, `{wifi_rede}`, `{wifi_senha}`, `{codigo}`. O `scheduler.py`:

- no **dia do check-in** envia ao hóspede a mensagem de boas-vindas / instruções
- na **véspera do check-out** envia o lembrete de saída

O `dashboard.py` corre o `scheduler` de 10 em 10 minutos numa thread de fundo; em
produção pode também correr-se por cron:

```bash
*/30 * * * *  cd /caminho/prototype && .venv/bin/python scheduler.py
```

Cada mensagem é enviada uma só vez por reserva (marcada em `reservations`) e fica
registada em *Mensagens*. Requer a funcionalidade *Mensagens proativas* e um
telefone na reserva.

### Envio de pedidos para prestadores

Quando um hóspede reporta um problema e o anfitrião tem **Envio automático** ativo
(por omissão):

1. `manager.py` classifica o problema numa categoria.
2. `dispatch.py` escolhe o primeiro prestador ativo cujas especialidades cobrem a
   categoria (recorrendo a um prestador `geral`) e atribui-lhe o pedido.
3. Esse prestador recebe uma mensagem de WhatsApp com o alojamento, morada e problema.
4. O prestador responde **`a caminho`** → o pedido passa a *em curso*;
   **`concluído`** → *concluído*. O `webhook.py` reconhece o prestador pelo número
   de telefone.
5. O anfitrião pode reatribuir qualquer pedido na página Pedidos — o novo prestador
   é avisado.

O WhatsApp de saída usa a API REST da Twilio quando `TWILIO_ACCOUNT_SID`,
`TWILIO_AUTH_TOKEN` e `TWILIO_WHATSAPP_FROM` estão definidos; caso contrário o
`notifications.py` **simula** o envio e imprime na consola, para que todo o fluxo
seja testável offline (o `demo.py` mostra-o). No sandbox da Twilio, o prestador
tem de ter aderido ao sandbox para receber uma mensagem real.

### Contas e sessões

- As palavras-passe são guardadas apenas como hash Werkzeug, nunca em texto simples.
- O registo exige um email válido e uma palavra-passe com 8+ caracteres.
- A **verificação de email** e a **recuperação de palavra-passe** enviam emails
  reais por SMTP (`mailer.py`) — qualquer fornecedor: Brevo, Resend, Postmark,
  Amazon SES, Mailgun… Configure `SMTP_HOST` / `SMTP_USER` / `SMTP_PASSWORD` /
  `MAIL_FROM` no `.env`. Sem SMTP configurado, os links vão para a consola do
  servidor (bom para desenvolvimento). Testar a configuração:
  ```bash
  .venv/bin/python mailer.py o-teu-email@exemplo.com
  ```
- Altere a palavra-passe a qualquer momento na página **Conta**.
- 5 tentativas de login erradas do mesmo IP → bloqueio de 60 segundos.
- Defina `FLASK_SECRET_KEY` no `.env` (qualquer string aleatória longa) para as
  sessões sobreviverem a reinícios do servidor. Carregue o `.env` antes de arrancar:
  ```bash
  export $(grep -v '^#' .env | xargs) && .venv/bin/python dashboard.py
  ```
- Ainda por fazer para produção: 2FA e verificação de domínio de envio (SPF/DKIM,
  no painel do fornecedor de email) — ver [docs/architecture.md](docs/architecture.md) §6.

## Consola de administração (operador da plataforma)

O operador (você) tem uma conta **admin**. Os admins não se auto-registam:

```bash
.venv/bin/python create_admin.py voce@exemplo.com          # pede uma palavra-passe
```

Entre no `/login` normal; um admin vai parar a **`/admin`** em vez de um painel de
anfitrião. A partir daí:

- **Adicionar cliente** — cria a conta de anfitrião e envia-lhe um email de
  boas-vindas com o link para definir a palavra-passe (email já confirmado).
- **Clientes** — todas as contas de anfitrião com plano, estado e número de
  alojamentos / pedidos / mensagens, mais um **feed de atividade recente** de
  todos os clientes.
- **Detalhe do cliente** — ver os alojamentos (com códigos do hóspede) e pedidos
  desse anfitrião, e gerir a conta:
  - **Suspender / reativar** — um anfitrião suspenso não consegue entrar e o
    assistente de WhatsApp deixa de responder.
  - **Plano** — Inicial (1 alojamento · 10€) / Intermédio (5 · 25€) / Pro
    (ilimitado · 99€). Não há plano gratuito. O limite é aplicado quando o
    anfitrião adiciona um alojamento. Se o
    anfitrião pediu um plano na página *Faturação*, aparece aqui um botão
    **Aplicar pedido** e um aviso em *Clientes*.
  - **Funcionalidades** — *Respostas automáticas por IA*, *Canal WhatsApp*,
    *Criação automática de pedidos*, *Envio automático a prestadores*,
    *Mensagens proativas*, *Rever respostas antes de enviar*. O `webhook.py` e o
    `scheduler.py` respeitam-nas em tempo real: por exemplo, com a IA desligada,
    todas as mensagens dos hóspedes são apenas reencaminhadas para o anfitrião.
- **Analytics do cliente** — no detalhe de cada cliente: mensagens (total / 7
  dias), taxa de resposta por IA, reservas, e pedidos por categoria.

O admin demo semeado é `admin@example.com` / `admin1234`.

### Segurança do webhook de WhatsApp

Defina `TWILIO_AUTH_TOKEN` (da consola Twilio) no `.env`. O `webhook.py` verifica
então a `X-Twilio-Signature` de cada pedido e devolve **403** a tudo o que não
venha mesmo da Twilio. Sem isso, o webhook regista um aviso e aceita pedidos não
assinados (só aceitável nos primeiros testes locais).

## Ligar o WhatsApp real

O [`prototype/whatsapp.py`](prototype/whatsapp.py) abstrai o canal e suporta
**dois fornecedores** — sem nenhum configurado, o envio é simulado para a
consola (o `demo.py` corre assim).

| | Twilio | **Meta Cloud API** |
|---|---|---|
| Custo | ~0,005 €/msg (precisa de cartão) | grátis até 1000 conversas/mês |
| Setup | conta Twilio + sandbox | conta Facebook + app Meta (~15 min) |
| Variáveis | `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_WHATSAPP_FROM` | ver [docs/whatsapp-meta.md](docs/whatsapp-meta.md) |

O webhook (`/hooks/whatsapp` em produção, `/whatsapp` local) trata dos dois
formatos: TwiML síncrono para o Twilio, verificação por GET + envio pela Graph
API para a Meta. Assinaturas validadas em ambos.

Depois de configurado: o hóspede envia uma mensagem, o assistente pede o
**código do hóspede** do alojamento (na página Alojamentos, ex.: `8f7bc6`), e a
partir daí responde às perguntas. A ligação número → alojamento fica na tabela
`guest_sessions`.

**Limite comum**: respostas livres só nas 24 h após a última mensagem do
hóspede — as respostas do assistente e dos prestadores estão sempre dentro
disso; as mensagens proativas de check-in (fora da janela) precisam mais tarde
de um template aprovado.

## Estado

Protótipo / prova de conceito. Interface e mensagens em português de Portugal.

- ✅ Q&A com hóspedes + criação de pedidos (`manager.py`)
- ✅ WhatsApp de entrada/saída — Twilio **ou** Meta Cloud API, com verificação de assinatura (`whatsapp.py` / `webhook.py`)
- ✅ Painel do anfitrião — registo/login, mensagens, fila de pedidos, CRUD de alojamentos (`dashboard.py`)
- ✅ Base de dados SQLite multi-tenant, todas as queries isoladas por anfitrião (`db.py`)
- ✅ Hash de palavras-passe, cookie de sessão, trava anti-força-bruta (`auth.py`)
- ✅ Emails transacionais reais por SMTP — verificação de email e recuperação de palavra-passe (`mailer.py`)
- ✅ Consola de admin — criar/gerir clientes (suspender, plano, funcionalidades), feed de atividade, emails de boas-vindas
- ✅ Notificação ao anfitrião por email quando há uma resposta para rever
- ✅ Envio de pedidos — atribuição automática a um prestador por especialidade, WhatsApp, atualizações de estado pelas respostas (`dispatch.py`)
- ✅ Fila de revisão — a IA redige, o anfitrião aprova/edita antes de enviar ao hóspede (`manager._plan` / `apply_review`)
- ✅ Importar anúncio — colar texto do Airbnb, pré-preencher campos do alojamento com a Claude (`listing_import.py`)
- ✅ Mensagens proativas — reservas + modelos por alojamento, envio no check-in e véspera de check-out (`scheduler.py`)
- ✅ Analytics por cliente na consola de admin (mensagens, taxa de IA, pedidos por categoria)
- ✅ Faturação com **Stripe** — checkout, portal do cliente, webhooks que sincronizam o plano (`billing.py`); sem chaves Stripe, cai no fluxo "pedir plano → admin aplica"
- ✅ Pronto para deploy — Postgres via `DATABASE_URL`, `gunicorn wsgi:app`, blueprint do Render (`render.yaml`, `docs/deploy.md`)
- 🚀 **Pronto para piloto** — runbook do operador em [docs/pilot.md](docs/pilot.md), guia do anfitrião em [docs/manual-anfitriao.md](docs/manual-anfitriao.md)
- ⬜ Sem ligação à caixa de entrada do Airbnb (precisa de integração com PMS — Hostaway/Guesty/Hospitable)
- ⬜ Sem 2FA, sem pagamento real (Stripe), sem apagar dados de hóspede a pedido (RGPD)

Ver [docs/architecture.md](docs/architecture.md) para o caminho até produção.
