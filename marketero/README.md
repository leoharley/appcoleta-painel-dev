# Marketero

Chatbot de WhatsApp com IA (Claude ou ChatGPT), coleta de contatos em sites e campanhas de disparo pela
**API oficial do WhatsApp (Cloud API da Meta)**. Publicado em `https://institutoalveslima.com.br/marketero`.

É independente do painel CodeIgniter deste repositório: Python 3.12 + FastAPI + SQLite, rodando como
serviço próprio (`marketero.service`) atrás do Apache.

## Funcionalidades

- **Conversas**: o webhook recebe as mensagens; a IA responde usando as instruções e a base de conhecimento
  das Configurações (e o contexto da campanha que originou a conversa). Pedido de proposta ou de atendimento
  humano marca a conversa com o alerta "precisa de atendimento humano" (o chatbot continua respondendo); a
  equipe pausa o chatbot manualmente quando assume. O alerta some quando a equipe responde, pausa o chatbot
  ou clica em "Marcar como resolvido".
- **Tempo de resposta** (Configurações › Atendimento do chatbot): espera configurável para a primeira resposta
  (sem resposta à pessoa nas últimas 24 h) e para as demais, contada da última mensagem recebida; mensagens
  enviadas nesse intervalo são respondidas juntas.
- **Descadastro**: mensagens como `SAIR` descadastram o contato (não recebe mais campanhas nem respostas).
- **Contatos**: cadastro, etiquetas, importação/exportação CSV, normalização de telefones brasileiros.
- **Importação por coleta**: fonte *TSE — partidos registrados* (contatos institucionais dos diretórios
  nacionais) e fonte genérica *página da internet*. Quando o site bloqueia acesso automático (o portal da
  Justiça Eleitoral bloqueia servidores), envie as páginas salvas pelo navegador (Ctrl+S).
- **Campanhas**: template aprovado pela Meta + variáveis por contato ({nome}, {organizacao}…), público por
  etiquetas, ritmo por minuto, janela de horário, limite diário, acompanhamento de entregue/lido/respondeu.
- **Templates** (canal Meta): cadastro pela tela Templates (cabeçalho, texto com variáveis, rodapé, botões de
  resposta rápida e de link), situação na Meta (aprovado, em análise, rejeitado com o motivo) e exclusão.
  Com o canal Kentro, os templates são cadastrados no próprio Kentro.
- **Modo simulação** (ligado por padrão): tudo funciona, mas nada sai pelo WhatsApp.

## Estrutura

```
app/main.py            aplicação FastAPI (rotas, sessão, disparador em segundo plano)
app/chatbot.py         mensagens recebidas, IA, descadastro, passagem para humano, status do webhook
app/ai.py              Claude (SDK anthropic) e ChatGPT (SDK openai)
app/whatsapp.py        cliente da Cloud API e validação de assinatura do webhook
app/dispatcher.py      envio das campanhas (ritmo, horário, limite diário)
app/scrapers/          extração de contatos (base genérica + fonte TSE)
app/routes/            telas e webhook
app/settings_store.py  configurações (segredos criptografados no banco)
deploy/                serviço systemd, Apache, backup e instalação
tests/smoke_test.py    teste de ponta a ponta em modo simulação
```

## Deploy (VPS)

```bash
# no servidor, a partir da pasta marketero/ enviada:
bash deploy/install.sh
# primeiro usuário (ou troca de senha):
cd /opt/marketero && set -a && . /etc/marketero/marketero.env && set +a \
  && runuser -u marketero -- .venv/bin/python manage.py criar-usuario admin
```

O `install.sh` instala em `/opt/marketero`, dados em `/var/lib/marketero` (banco + backups diários),
variáveis em `/etc/marketero/marketero.env` (gerado na primeira vez, com `MARKETERO_SECRET_KEY` — não
perca: ela decifra os segredos gravados no banco). O vhost 443 do `institutoalveslima.com.br` inclui
`/etc/httpd/conf/marketero.conf`.

Logs: `journalctl -u marketero -f`

## Testes

Rodar todos antes de publicar (nenhum faz chamada real a WhatsApp, Kentro ou IA):

```bash
for t in smoke_test kentro_test review_test ai_wire_test modelos_test; do
  MARKETERO_DATA_DIR=$(mktemp -d) MARKETERO_COOKIE_SECURE=0 MARKETERO_SECRET_KEY=t .venv/bin/python -m tests.$t
done
```

- `smoke_test`: telas, importação, webhook da Meta, chatbot e campanha (modo simulação).
- `kentro_test`: integração com o Kentro contra servidor simulado (envio, status, leitura, respostas).
- `review_test`: regressões da revisão (telefones sem o 9, trava do banco, coletor sem endereços internos…).
- `modelos_test`: cadastro, validação e exclusão de templates (API da Meta simulada).
- `ai_wire_test`: confere o que os SDKs da Anthropic e da OpenAI enviam (corpo e cabeçalhos).
- `fake_kentro_server.py`: Kentro + OpenAI simulados para testar o sistema rodando de ponta a ponta.

## WhatsApp pelo Kentro (atenderbem) — canal padrão

O Marketero usa a API de integração do Kentro (`/int/...`, especificação em
`https://institutoalves.atenderbem.com/static/getAPPAPISpecs`). Em Configurações: canal **Kentro**,
**ID da fila** do WhatsApp e **chave da API** (global ou da fila).

- **Campanhas**: `enqueueMessageToSend` com o `templateId` dos templates cadastrados no Kentro (`getTemplates`);
  a situação de cada envio (na fila, enviada, entregue, lida, falha) vem de `checkEnqueuedMessage`.
- **Respostas**: a API não documenta os "Webhooks Gerais"; o Marketero lê `getAllOpenChats` +
  `getChatMessages` a cada N segundos (`app/kentro_poller.py`) e responde com `sendMessageToChat`.
- O robô só responde atendimentos **aguardando na fila** (sem atendente, fora da URA) e mensagens recebidas
  há no máximo 30 min depois da ativação. Quando sinaliza a equipe, deixa nota interna (`info=true`) e
  continua respondendo até um atendente assumir.
  A API não permite transferir atendimentos; quem distribui é a própria fila do Kentro.
- Erros `AUTH_021` = IP não autorizado: libere o IP do servidor na aba Segurança do Kentro.

Teste: `MARKETERO_DATA_DIR=$(mktemp -d) .venv/bin/python -m tests.kentro_test` (servidor Kentro simulado).

## Configuração na Meta (só se usar a Cloud API direto, sem o Kentro)

1. developers.facebook.com → app → WhatsApp → Configuração da API: copie **Phone Number ID** e
   **WhatsApp Business Account ID**; gere um **token permanente** (usuário do sistema no Business Manager,
   permissões `whatsapp_business_messaging` e `whatsapp_business_management`).
2. Configurações do app → Básico: **Chave secreta do app** (App Secret).
3. WhatsApp → Configuração → Webhook: URL `https://institutoalveslima.com.br/marketero/webhook/whatsapp`,
   token de verificação = o mostrado nas Configurações do Marketero; assine o campo **messages**.
4. Crie e aprove os **templates** de mensagem (o primeiro contato com cada pessoa só pode ser por template).
5. No Marketero, desligue o **modo simulação**.
