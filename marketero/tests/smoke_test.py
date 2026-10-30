"""Teste de ponta a ponta em modo simulação (sem WhatsApp real e com IA falsa).

  MARKETERO_DATA_DIR=$(mktemp -d) MARKETERO_COOKIE_SECURE=0 python -m tests.smoke_test
"""
import asyncio
import hashlib
import hmac
import json
import re
import sys

from fastapi.testclient import TestClient

from app import ai, chatbot, dispatcher
from app import settings_store as cfg
from app.db import db_session, init_db
from app.main import app
from app.models import (Campaign, CampaignRecipient, Contact, Conversation,
                        Message)
from app.scrapers import extract_records

failures = []


def check(cond, label):
    print(("OK   " if cond else "FALHA"), label)
    if not cond:
        failures.append(label)


def csrf(html: str) -> str:
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


PROMPTS = []


async def fake_reply(conf, system, history, system_dynamic=""):
    PROMPTS.append((system, system_dynamic))
    last = history[-1]["content"]
    if "proposta" in last.lower():
        return "Perfeito! Vou passar para a nossa equipe comercial. [[HUMANO]]"
    return f"Resposta da IA para: {last}"


ai.generate_reply = fake_reply
ai.AIConfig.configured = property(lambda self: True)
REAL_REPLY_DELAY = chatbot.reply_delay
chatbot.reply_delay = lambda db, conv: 0

TSE_SAMPLE = """<html><body><main>
<h1>Partidos políticos registrados no TSE</h1>
<h2>Partido Exemplo (PEX)</h2>
<p>Presidente nacional: Fulano de Tal</p>
<p>Endereço: SHS Quadra 6, Brasília/DF</p>
<p>Telefone: (61) 3333-4444 / (61) 99876-5432</p>
<p>E-mail: contato@pex.org.br</p><p>Site: www.pex.org.br</p>
<h2>Outro Partido (OPT)</h2>
<p>Telefone: (11) 2222-3333</p><p>E-mail: nacional@opt.org.br</p>
</main></body></html>"""


def main():
    init_db()
    with TestClient(app) as c:
        # --- usuário e login ---
        from manage import criar_usuario
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            criar_usuario("admin")
        password = re.search(r"Senha: (\S+)", buf.getvalue()).group(1)
        r = c.get("/")
        check(r.url.path.endswith("/login"), "sem login redireciona para /login")
        tok = csrf(c.get("/login").text)
        r = c.post("/login", data={"csrf_token": tok, "username": "admin", "password": "errada"})
        check("inválidos" in r.text, "senha errada é recusada")
        tok = csrf(c.get("/login").text)
        r = c.post("/login", data={"csrf_token": tok, "username": "admin", "password": password})
        check(r.status_code == 200 and "Painel" in r.text, "login e painel")
        for path in ("/contatos", "/contatos/novo", "/importar", "/campanhas", "/campanhas/nova", "/conversas",
                     "/configuracoes", "/senha"):
            check(c.get(path).status_code == 200, f"página {path}")
        r = c.post("/configuracoes", data={"wa_simulation": "1"})
        check("expirado" in r.text, "POST sem CSRF é recusado")

        # --- configurações ---
        tok = csrf(c.get("/configuracoes").text)
        r = c.post("/configuracoes", data={"csrf_token": tok, "wa_provider": "meta",
                                           "ai_provider": "anthropic", "anthropic_api_key": "sk-teste",
                                           "wa_app_secret": "segredo", "wa_access_token": "tok", "wa_phone_number_id": "123",
                                           "wa_simulation": "on", "bot_enabled": "on", "dispatch_hour_start": "0",
                                           "dispatch_hour_end": "24", "dispatch_weekends": "on"})
        check("Configurações salvas" in r.text, "salvar configurações")
        with db_session() as db:
            check(cfg.get(db, "anthropic_api_key") == "sk-teste", "segredo gravado e lido")
            from app.models import Setting
            check(db.get(Setting, "anthropic_api_key").value.startswith("enc:"), "segredo criptografado no banco")
            verify_token = cfg.get(db, "wa_verify_token")

        # --- subtelas de configurações ---
        for sec, field in (("ia", "ai_provider"), ("atendimento", "reply_delay_first"), ("whatsapp", "wa_provider"),
                           ("disparos", "dispatch_daily_limit")):
            page = c.get(f"/configuracoes/{sec}").text
            check(f'name="{field}"' in page and f'value="{sec}"' in page, f"subtela {sec}")
        check('name="dispatch_daily_limit"' not in c.get("/configuracoes/ia").text, "subtela mostra só os seus campos")
        check("Base de conhecimento adicional" in c.get("/configuracoes/atendimento").text, "arquivos .md no atendimento")
        check(c.get("/configuracoes/inexistente", follow_redirects=False).status_code == 303, "subtela inválida volta")
        tok = csrf(c.get("/configuracoes/disparos").text)
        r = c.post("/configuracoes", data={"csrf_token": tok, "section": "disparos", "dispatch_daily_limit": "100",
                                           "dispatch_hour_start": "0", "dispatch_hour_end": "24", "dispatch_weekends": "on",
                                           "optout_keywords": "SAIR, PARAR", "optout_reply": "Ok."})
        with db_session() as db:
            check(cfg.get(db, "dispatch_daily_limit") == "100" and cfg.get_bool(db, "wa_simulation")
                  and cfg.get_bool(db, "bot_enabled"), "salvar uma subtela não desliga interruptores das outras")
        check('value="disparos"' in r.text, "depois de salvar continua na mesma subtela")
        tok = csrf(r.text)
        c.post("/configuracoes", data={"csrf_token": tok, "section": "atendimento", "bot_enabled": "on",
                                       "reply_delay_first": "30", "reply_delay_next": "9999", "history_messages": "20"})
        with db_session() as db:
            check(cfg.get(db, "reply_delay_first") == "30" and cfg.get(db, "reply_delay_next") == "600",
                  "tempos de resposta salvos (máximo 600 s)")
            ct = Contact(name="Tempo", phone="5538911112222")
            db.add(ct)
            db.flush()
            cv = Conversation(contact_id=ct.id)
            db.add(cv)
            db.flush()
            check(REAL_REPLY_DELAY(db, cv) == 30, "primeira resposta usa o tempo da primeira")
            db.add(Message(conversation_id=cv.id, direction="out", author="bot", kind="text", body="oi", status="sent"))
            db.flush()
            check(REAL_REPLY_DELAY(db, cv) == 600, "respostas seguintes usam o outro tempo")
            db.rollback()
            cfg.set_value(db, "reply_delay_first", "4")
            cfg.set_value(db, "reply_delay_next", "4")
            db.commit()

        # --- base de conhecimento .md ---
        tok = csrf(c.get("/configuracoes").text)
        md = "# Preços\n\nPesquisa de intenção de voto: a partir de R$ 9.900.\n".encode("utf-8")
        r = c.post("/configuracoes/conhecimento", data={"csrf_token": tok},
                   files=[("files", ("precos.md", md, "text/markdown")),
                          ("files", ("planilha.xlsx", b"x", "application/octet-stream"))])
        check("1 documento(s) importado(s)" in r.text and "precos.md" in r.text, "importar .md")
        check("planilha.xlsx: use arquivos .md" in r.text, "arquivo que não é .md é recusado")
        tok = csrf(r.text)
        r = c.post("/configuracoes/conhecimento", data={"csrf_token": tok},
                   files=[("files", ("precos.md", "# Preços\n\nA partir de R$ 12.000.".encode(), "text/markdown"))])
        check("1 substituído(s)" in r.text, "reimportar .md com o mesmo nome substitui")
        with db_session() as db:
            from app.models import KnowledgeDoc
            doc = db.query(KnowledgeDoc).filter_by(name="precos.md").one()
            static = chatbot.build_static_prompt(db)
            check("R$ 12.000" in static and "R$ 9.900" not in static, "documento entra nas instruções da IA")
            doc_id = doc.id
        check(c.get(f"/configuracoes/conhecimento/{doc_id}").text.startswith("# Preços"), "visualizar documento")
        tok = csrf(c.get("/configuracoes").text)
        c.post(f"/configuracoes/conhecimento/{doc_id}", data={"csrf_token": tok, "action": "toggle"})
        with db_session() as db:
            check("R$ 12.000" not in chatbot.build_static_prompt(db), "documento desativado sai das instruções")
        tok = csrf(c.get("/configuracoes").text)
        c.post(f"/configuracoes/conhecimento/{doc_id}", data={"csrf_token": tok, "action": "toggle"})

        # --- importação CSV ---
        tok = csrf(c.get("/importar").text)
        csv_data = "nome;partido;whatsapp;email;cidade;uf\nMaria Souza;PXX;(38) 99999-1111;maria@x.org;Montes Claros;mg\n" \
                   "Sem Telefone;PYY;;sem@y.org;BH;MG\nInválido;PZZ;123;;;\n"
        r = c.post("/contatos/importar-csv", data={"csrf_token": tok, "tags": "teste"},
                   files={"file": ("lista.csv", csv_data.encode(), "text/csv")})
        check("2 novos" in r.text, "importação CSV (2 válidos, 1 inválido)")

        # --- leitura de página no formato do TSE ---
        recs = extract_records(TSE_SAMPLE, "teste")
        check(len(recs) == 2, f"TSE: 2 partidos extraídos (veio {len(recs)})")
        if recs:
            p = recs[0]
            check(p.phones and p.phones[0] == "5561998765432", "TSE: celular priorizado")
            check(p.email == "contato@pex.org.br" and "pex.org.br" in p.website, "TSE: e-mail e site")
            check(p.name == "Fulano de Tal" and "PEX" in p.organization, "TSE: presidente e sigla")
        tok = csrf(c.get("/importar").text)
        r = c.post("/importar/arquivos", data={"csrf_token": tok, "source": "tse_partidos", "tags": "partido, tse"},
                   files=[("files", ("partidos.html", TSE_SAMPLE.encode(), "text/html"))])
        check("2 novos" in r.text, "TSE: envio de página salva cria os contatos")

        # --- webhook ---
        r = c.get("/webhook/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": verify_token,
                                               "hub.challenge": "42"})
        check(r.text == "42", "webhook: verificação da Meta")
        check(c.get("/webhook/whatsapp", params={"hub.mode": "subscribe", "hub.verify_token": "x",
                                                 "hub.challenge": "1"}).status_code == 403, "webhook: token errado")

        def post_hook(payload):
            raw = json.dumps(payload).encode()
            sig = "sha256=" + hmac.new(b"segredo", raw, hashlib.sha256).hexdigest()
            return c.post("/webhook/whatsapp", content=raw,
                          headers={"x-hub-signature-256": sig, "content-type": "application/json"})

        def inbound(text, wamid, wa_id="5538988887777"):
            return {"entry": [{"changes": [{"value": {
                "contacts": [{"wa_id": wa_id, "profile": {"name": "João Teste"}}],
                "messages": [{"from": wa_id, "id": wamid, "type": "text", "text": {"body": text}}]}}]}]}

        check(c.post("/webhook/whatsapp", content=b"{}", headers={"x-hub-signature-256": "sha256=00"}).status_code == 401,
              "webhook: assinatura inválida recusada")
        check(post_hook(inbound("Olá, quero saber dos serviços", "wamid.1")).status_code == 200, "webhook: mensagem aceita")

        # Processa diretamente (o webhook dispara em segundo plano)
        asyncio.run(chatbot.handle_inbound("5538988887777", "João Teste",
                                           {"id": "wamid.2", "type": "text", "text": {"body": "Quero uma proposta"}}))
        with db_session() as db:
            conv = db.query(Conversation).join(Contact).filter(Contact.phone == "5538988887777").one()
            bodies = [m.body for m in conv.messages]
            check(any("equipe comercial" in b for b in bodies), "chatbot respondeu com a IA")
            check(conv.needs_human and not conv.bot_paused,
                  "pedido de proposta sinaliza atendimento humano sem pausar o chatbot")
            check(all(HANDOFF not in b for b in bodies), "marcador [[HUMANO]] não vai para o contato")
            static, dynamic = PROMPTS[-1]
            check("R$ 12.000" in static and "João Teste" in dynamic and "João Teste" not in static,
                  "IA recebe base .md na parte fixa e dados do contato na parte variável")
            conv_id = conv.id
        page = c.get(f"/conversas/{conv_id}")
        check(page.status_code == 200 and "Precisa de atendimento humano" in page.text, "tela da conversa com o alerta")
        check("precisa de atendimento humano" in c.get("/conversas").text, "lista mostra o alerta")
        tok = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        c.post(f"/conversas/{conv_id}/bot", data={"csrf_token": tok, "action": "resolve"})
        with db_session() as db:
            conv = db.get(Conversation, conv_id)
            check(not conv.needs_human and not conv.bot_paused, "marcar como resolvido tira o alerta e mantém o chatbot")

        asyncio.run(chatbot.handle_inbound("5538977776666", "Ana",
                                           {"id": "wamid.3", "type": "text", "text": {"body": "Sair"}}))
        with db_session() as db:
            ana = db.query(Contact).filter(Contact.phone == "5538977776666").one()
            check(ana.opted_out, "descadastro por palavra-chave")

        # --- campanha ---
        tok = csrf(c.get("/campanhas/nova").text)
        r = c.post("/campanhas/criar", data={"csrf_token": tok, "name": "Teste", "template_name": "apresentacao",
                                             "template_language": "pt_BR", "template_body": "Olá {{1}}, do {{2}}!",
                                             "variables": "{nome}\n{organizacao}", "tags": "teste", "rate_per_minute": "12"})
        check("destinatário" in r.text, "campanha criada")
        with db_session() as db:
            camp = db.query(Campaign).order_by(Campaign.id.desc()).first()
            n = db.query(CampaignRecipient).filter_by(campaign_id=camp.id).count()
            check(n == 1, f"público por etiqueta (1 com telefone; veio {n})")
            camp_id = camp.id
        tok = csrf(c.get(f"/campanhas/{camp_id}").text)
        c.post(f"/campanhas/{camp_id}/acao", data={"csrf_token": tok, "action": "start"})
        asyncio.run(dispatcher.tick())
        with db_session() as db:
            rec = db.query(CampaignRecipient).filter_by(campaign_id=camp_id).one()
            check(rec.status == "sent" and rec.wa_message_id.startswith("sim-"), "disparo simulado enviado")
            msg = db.query(Message).filter_by(wa_message_id=rec.wa_message_id).one()
            check(msg.body == "Olá Maria, do PXX!", f"template renderizado ({msg.body})")
            wamid = rec.wa_message_id
        post_hook({"entry": [{"changes": [{"value": {"statuses": [
            {"id": wamid, "status": "read", "recipient_id": "5538999991111"}]}}]}]})
        with db_session() as db:
            check(db.query(CampaignRecipient).filter_by(campaign_id=camp_id).one().status == "read", "status de leitura")
        asyncio.run(dispatcher.tick())
        with db_session() as db:
            check(db.get(Campaign, camp_id).status == "finished", "campanha concluída")
        check(c.get(f"/campanhas/{camp_id}").status_code == 200, "tela da campanha")
        check(c.get("/contatos/exportar.csv").status_code == 200, "exportar CSV")

    print("\n" + ("TUDO OK" if not failures else f"{len(failures)} FALHA(S): {failures}"))
    sys.exit(1 if failures else 0)


HANDOFF = chatbot.HANDOFF_MARK

if __name__ == "__main__":
    main()
