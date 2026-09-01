# WhatsApp via Meta Cloud API

Alternativa ao Twilio. **Grátis** até 1000 conversas/mês. Precisa de uma conta
Facebook e ~15 min de configuração no painel da Meta.

O código já suporta os dois fornecedores (`prototype/whatsapp.py`); aqui só se
liga a Meta.

## 1. Criar a app na Meta

1. https://developers.facebook.com/ → entra com a tua conta Facebook.
2. **My Apps → Create App** → tipo **Business** → dá-lhe um nome.
3. No painel da app: **Add product → WhatsApp → Set up**.
4. Se pedir, cria/escolhe um **Meta Business Account**.

## 2. Número de teste + credenciais

Na página **WhatsApp → API Setup**:

- **From** — a Meta dá-te um **número de teste** grátis. `Phone number ID`
  aparece por baixo → é o `META_PHONE_NUMBER_ID`.
- **Temporary access token** — serve para testar (expira em 24 h). Para produção,
  cria um **token permanente** (passo 5).
- **To** — adiciona o **teu** número de telemóvel como destinatário de teste
  (recebes um código no WhatsApp para confirmar). No sandbox só podes enviar
  para números adicionados aqui.

## 3. Webhook

Na página **WhatsApp → Configuration** (ou **API Setup → Configure webhooks**):

- **Callback URL**: `https://gestor-alojamento.onrender.com/hooks/whatsapp`
- **Verify token**: inventa uma string (ex.: `gestor-aloj-2026-xyz`) — tem de
  ser **igual** à variável `META_VERIFY_TOKEN` no Render.
- **Verify and save** — a Meta faz um GET ao teu endpoint; se o token bater,
  fica verde.
- Em **Webhook fields**, subscreve **`messages`**.

## 4. App Secret (para validar assinatura)

**App dashboard → Settings → Basic → App Secret → Show**. É o `META_APP_SECRET`.
(Enquanto não o defines, o webhook aceita tudo — mete-o antes de usar a sério.)

## 5. Token permanente (produção)

O token temporário expira em 24 h. Para um que não expire:

1. **Business Settings** (business.facebook.com) → **Users → System Users** →
   **Add** → nome, role *Admin*.
2. Nesse system user: **Add Assets** → a tua app WhatsApp → controlo total.
3. **Generate new token** → escolhe a app → permissões
   **`whatsapp_business_messaging`** e **`whatsapp_business_management`** →
   *Never expires* → **Generate**. Guarda-o.

## 6. Variáveis no Render (serviço web → Environment)

| KEY | VALUE |
|---|---|
| `WHATSAPP_PROVIDER` | `meta` |
| `META_ACCESS_TOKEN` | o token (permanente, passo 5) |
| `META_PHONE_NUMBER_ID` | da página API Setup |
| `META_VERIFY_TOKEN` | a string que puseste no webhook (passo 3) |
| `META_APP_SECRET` | passo 4 |

**Save Changes** → redeploy. Reconfirma o webhook no painel Meta (passo 3) se
mudaste o `META_VERIFY_TOKEN` depois.

## 7. Testar

Do número que adicionaste como destinatário de teste, envia uma mensagem para o
número de teste da Meta. Deves receber:

> *Olá! Sou o assistente da sua estadia. Qual é o código do hóspede…*

Depois envia um código de alojamento (com `SEED_DEMO=1`, ex.: `03d3a7`).

## Limites a saber

- **Janela de 24 h**: só podes enviar mensagem livre até 24 h após a última
  mensagem do hóspede. As respostas do assistente e dos prestadores estão sempre
  dentro disso. As **mensagens proativas** de check-in (o hóspede ainda não
  escreveu) caem fora → a Meta rejeita-as. Para essas, mais tarde: criar um
  **template** aprovado. Por agora o `scheduler.py` tenta e regista o erro.
- **Número de teste da Meta**: só envia para destinatários adicionados à mão.
  Para abrir a toda a gente, adiciona um número próprio e verifica o negócio
  (processo da Meta, uns dias).
