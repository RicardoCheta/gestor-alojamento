# Runbook do piloto (operador)

Objetivo: **1 anfitrião real**, os alojamentos dele, hóspedes reais por WhatsApp,
**fila de revisão ligada** nas primeiras 2–3 semanas. Valida que o produto ajuda
e que alguém paga.

## Antes de começar

- [ ] Deploy feito e a responder — ver [deploy.md](deploy.md). `https://…/login` abre.
- [ ] `APP_BASE_URL` correto (senão os links dos emails vão para localhost).
- [ ] Email a funcionar: `python mailer.py o-teu-email` recebe.
- [ ] WhatsApp: sandbox da Twilio ligado a `https://…/hooks/whatsapp`, **ou** já
      tens número próprio aprovado (define `TWILIO_ACCOUNT_SID` /
      `TWILIO_AUTH_TOKEN` / `TWILIO_WHATSAPP_FROM`). Com número próprio, os
      hóspedes não têm de fazer "join".
- [ ] A tua conta de admin criada: `python create_admin.py tu@dominio.pt`.

## Onboarding do anfitrião

1. **/admin → Adicionar cliente** — nome + email. A conta é criada (email já
   confirmado, porque tu o validaste) e o anfitrião recebe um email para definir
   a palavra-passe.
2. **Plano**: no detalhe do cliente, escolhe o plano (Inicial / Intermédio / Pro)
   conforme o nº de alojamentos.
3. **Liga "Rever respostas antes de enviar"** nas Funcionalidades. Deixa as
   outras como estão.
4. Pede ao anfitrião para, no painel dele:
   - **Alojamentos → Adicionar alojamento** (ou colar o texto do anúncio Airbnb
     para pré-preencher). Preencher WiFi, AC, estacionamento, check-in/out,
     regras. Rever as **Mensagens automáticas**.
   - Copiar o **código do hóspede** de cada alojamento.
   - **Reservas → Adicionar** as reservas ativas e próximas (nome, telefone
     WhatsApp, datas) — para as mensagens de check-in/out.
   - (Opcional) **Prestadores** — nome, WhatsApp e especialidades dos
     contactos de limpeza / manutenção.
5. **Guião para os hóspedes**: o anfitrião mete o código do hóspede nas mensagens
   automáticas do Airbnb / na mensagem de reserva. Ex.: *"Para falar com o
   assistente do apartamento, envie a palavra `8f7bc6` para o número +351…"*.
   (Com número Twilio próprio; com sandbox, incluir também o "join".)

O anfitrião tem uma versão desta lista em
[manual-anfitriao.md](manual-anfitriao.md).

## Durante o piloto

- **Fila de revisão**: o anfitrião recebe um email por cada mensagem em espera e
  aprova/edita no painel. Combina com ele um tempo-alvo de resposta (ex.: 30 min
  em horário de dia).
- **Monitorização (tu)**: `/admin` mostra a atividade recente de todos os
  clientes e os contadores. Verifica 1x/dia:
  - respostas na fila há muito tempo → falar com o anfitrião
  - respostas erradas aprovadas → ajustar a informação do alojamento
  - pedidos de manutenção mal classificados → idem
- **Logs**: erros aparecem no log do serviço (Render → Logs). `[mailer]` e
  `[WhatsApp send failed]` indicam problemas de entrega.

## Fim das 2–3 semanas — decisão

Com o anfitrião, revê:
- Quantas mensagens? Que % o assistente acertou (aprovadas sem edição)?
- Quanto tempo poupou por semana?
- Pagaria o plano X?

Se sim: **desliga a fila de revisão** (respostas automáticas) e, se ainda não o
fizeste, liga o **Stripe** ([deploy.md §6](deploy.md)) — o anfitrião subscreve
na página *Faturação*. Se não: percebe o quê e itera, ou tenta outro perfil de
anfitrião.

## Reverter / pausar

- Suspender a conta do cliente em `/admin` → o assistente para de responder.
- Desligar `Canal WhatsApp` → só o webhook para; o painel continua.
