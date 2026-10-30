"""Testes de regressão dos problemas encontrados na revisão (um caso por problema).

  MARKETERO_DATA_DIR=$(mktemp -d) MARKETERO_COOKIE_SECURE=0 MARKETERO_SECRET_KEY=t python -m tests.review_test
"""
import asyncio
import contextlib
import hashlib
import hmac
import io
import json
import re
import sys
import time
from datetime import timedelta

from fastapi.testclient import TestClient

from app import ai, chatbot, dispatcher, messaging
from app import settings_store as cfg
from app.db import db_session, init_db
from app.main import app
from app.models import (Campaign, CampaignRecipient, Contact, Conversation,
                        Message, Setting)
from app.phones import normalize
from app.scrapers.base import fetch

failures = []


def check(cond, label):
    print(("OK   " if cond else "FALHA"), label)
    if not cond:
        failures.append(label)


def csrf(html):
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


async def run_async_checks():
    # --- telefones: 9º dígito dos celulares brasileiros ---
    check(normalize("+553899991111") == "5538999991111", "wa_id sem o 9 vira a forma com 9")
    check(normalize("(38) 9999-1111") == "5538999991111", "número local antigo (8 dígitos) ganha o 9")
    check(normalize("(38) 99999-1111") == "5538999991111", "celular atual sem mudança")
    check(normalize("(38) 3221-4000") == "553832214000", "telefone fixo não recebe 9")
    check(normalize("(55) 9999-1111") == "5555999991111", "DDD 55 (RS) tratado como número local")
    check(normalize("+1 202 555 0143") == "12025550143", "número estrangeiro sem mudança")

    with db_session() as db:
        for k, v in {"wa_provider": "meta", "wa_simulation": "1", "wa_access_token": "t", "wa_phone_number_id": "1",
                     "bot_enabled": "1", "dispatch_hour_start": "0", "dispatch_hour_end": "24",
                     "dispatch_weekends": "1"}.items():
            cfg.set_value(db, k, v)
        maria = Contact(name="Maria", phone="5538999991111", tags="t1")
        db.add(maria)
        db.commit()
        maria_id = maria.id

    # webhook da Meta com wa_id antigo encontra o contato importado (sem duplicar)
    ai.generate_reply = fake_reply
    ai.AIConfig.configured = property(lambda self: True)
    real_delay = chatbot.reply_delay
    chatbot.reply_delay = lambda db, conv: 0
    await chatbot.handle_inbound("553899991111", "Maria", {"id": "w1", "type": "text", "text": {"body": "Oi"}})
    with db_session() as db:
        n = db.query(Contact).filter(Contact.phone.in_(("553899991111", "5538999991111"))).count()
        check(n == 1, "mensagem com wa_id sem o 9 não duplica o contato")
        conv = db.query(Conversation).filter_by(contact_id=maria_id).one()
        check(any(m.body == "Oi" for m in conv.messages), "mensagem cai na conversa do contato importado")
    chatbot.handle_status({"id": "x", "status": "delivered", "recipient_id": "553899991111"})
    with db_session() as db:
        check(db.get(Contact, maria_id).whatsapp_status == "valid", "status do webhook com número sem o 9 atualiza o contato")

    # --- disparo: nenhuma escrita aberta durante a chamada externa ---
    class SlowChannel:
        key, label, simulation, configured = "meta", "teste", False, True

        async def send_template(self, contact, campaign, params, ref):
            t0 = time.time()
            with db_session() as other:  # outra parte do sistema gravando durante o envio
                cfg.set_value(other, "teste_concorrencia", str(t0))
                other.commit()
            self.write_seconds = time.time() - t0
            return "w-sent", "sent"

    slow = SlowChannel()
    orig_get_channel = dispatcher.get_channel
    dispatcher.get_channel = lambda db: slow
    with db_session() as db:
        novo = Contact(name="Novo", phone="5538977776666", tags="t2")  # sem conversa ainda
        camp = Campaign(name="C", template_name="t", template_ref="t", status="running", rate_per_minute=12,
                        filter_tags="t2")
        db.add_all([novo, camp])
        db.flush()
        db.add(CampaignRecipient(campaign_id=camp.id, contact_id=novo.id))
        db.commit()
        camp_id = camp.id
    await dispatcher.tick()
    check(getattr(slow, "write_seconds", 99) < 2, f"outra gravação durante o envio não espera a trava do banco "
                                                    f"({getattr(slow, 'write_seconds', 99):.2f}s)")
    dispatcher.get_channel = orig_get_channel
    with db_session() as db:
        rec = db.query(CampaignRecipient).filter_by(campaign_id=camp_id).one()
        check(rec.status == "sent", "destinatário marcado como enviado")
        # envio interrompido no meio: não reenvia às cegas
        rec.status = "sending"
        db.commit()
        dispatcher.recover_interrupted(db)
        db.refresh(rec)
        check(rec.status == "failed" and "pode ter sido enviado" in rec.error, "envio interrompido vira falha com aviso")

    # --- campanha não reativa o robô de quem foi passado para a equipe ---
    with db_session() as db:
        conv = db.query(Conversation).filter_by(contact_id=maria_id).one()
        conv.bot_paused = True
        camp2 = Campaign(name="C2", template_name="t", template_ref="t", status="running", rate_per_minute=12)
        db.add(camp2)
        db.flush()
        db.add(CampaignRecipient(campaign_id=camp2.id, contact_id=maria_id))
        db.commit()
    dispatcher._last_send.clear()
    await dispatcher.tick()
    with db_session() as db:
        check(db.query(Conversation).filter_by(contact_id=maria_id).one().bot_paused,
              "envio de campanha mantém o robô pausado")

    # --- notas internas fora do histórico da IA ---
    with db_session() as db:
        conv = db.query(Conversation).filter_by(contact_id=maria_id).one()
        db.add(Message(conversation_id=conv.id, direction="out", author="bot", kind="system", status="sent",
                       body="[Atendimento passado para a equipe]"))
        db.commit()
        hist = chatbot.history_for_ai(db, conv)
        check(not any("passado para a equipe" in h["content"] for h in hist), "nota interna não vai para a IA")

    # --- resposta descartada se alguém assumiu durante a geração ---
    async def reply_and_pause(conf, system, history, system_dynamic=""):
        with db_session() as other:
            c = other.query(Conversation).filter_by(contact_id=maria_id).one()
            c.bot_paused = True
            other.commit()
        return "resposta atrasada"

    with db_session() as db:
        conv = db.query(Conversation).filter_by(contact_id=maria_id).one()
        conv.bot_paused = False
        db.commit()
        ai.generate_reply = reply_and_pause
        res = await chatbot.reply_with_ai(db, conv)
        ai.generate_reply = fake_reply
        check(res is None and not any(m.body == "resposta atrasada" for m in conv.messages),
              "resposta da IA descartada quando a conversa foi assumida durante a geração")

    # --- Kentro: espera o tempo de resposta contado da última mensagem do contato ---
    from app import kentro_poller
    chatbot.reply_delay = real_delay
    with db_session() as db:
        cfg.set_value(db, "reply_delay_first", "2")
        cfg.set_value(db, "reply_delay_next", "2")
        conv = db.query(Conversation).filter_by(contact_id=maria_id).one()
        conv.last_inbound_at = chatbot.utcnow()
        db.commit()
        conv_id = conv.id
    t0 = time.time()
    await kentro_poller._wait_reply_delay(conv_id)
    waited = time.time() - t0
    check(1.5 < waited < 4, f"Kentro espera o tempo configurado antes de responder ({waited:.1f}s)")
    with db_session() as db:
        db.get(Conversation, conv_id).last_inbound_at = chatbot.utcnow() - timedelta(minutes=5)
        db.commit()
    t0 = time.time()
    await kentro_poller._wait_reply_delay(conv_id)
    check(time.time() - t0 < 0.5, "mensagem antiga (lida com atraso do Kentro) não espera de novo")
    chatbot.reply_delay = lambda db, conv: 0

    # --- coletor de páginas: endereços internos bloqueados ---
    for url in ("http://127.0.0.1:8100/", "http://localhost/", "http://169.254.169.254/latest/meta-data/",
                "http://10.0.0.1/", "https://exemplo.com:8443/", "file:///etc/passwd"):
        try:
            await fetch(url)
            check(False, f"coletor bloqueia {url}")
        except Exception as e:
            check(isinstance(e, ValueError), f"coletor bloqueia {url} ({e})")


async def fake_reply(conf, system, history, system_dynamic=""):
    return "Resposta: " + history[-1]["content"]


def web_checks():
    with TestClient(app) as c:
        from manage import criar_usuario
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            criar_usuario("admin")
        password = re.search(r"Senha: (\S+)", buf.getvalue()).group(1)

        # limite de login não é burlado trocando o X-Forwarded-For
        for i in range(5):
            tok = csrf(c.get("/login").text)
            c.post("/login", data={"csrf_token": tok, "username": "admin", "password": "x"},
                   headers={"x-forwarded-for": f"10.0.0.{i}"})
        tok = csrf(c.get("/login").text)
        r = c.post("/login", data={"csrf_token": tok, "username": "admin", "password": password},
                   headers={"x-forwarded-for": "10.9.9.9"})
        check("Muitas tentativas" in r.text, "X-Forwarded-For forjado não burla o limite de tentativas")
        from app import security
        security._failures.clear()
        tok = csrf(c.get("/login").text)
        c.post("/login", data={"csrf_token": tok, "username": "admin", "password": password})

        # etiqueta exata e CSV de uma coluna
        tok = csrf(c.get("/importar").text)
        r = c.post("/contatos/importar-csv", data={"csrf_token": tok, "tags": "PTB"},
                   files={"file": ("uma.csv", "telefone\n(38) 98111-2222\n(38) 98111-3333\n".encode(), "text/csv")})
        check("2 novos" in r.text, "CSV com uma coluna só é importado")
        tok = csrf(r.text)
        r = c.post("/contatos/importar-csv", data={"csrf_token": tok, "tags": "PT"},
                   files={"file": ("pt.csv", "nome;telefone\n=HYPERLINK(\"x\");(38) 98111-4444\n".encode(), "text/csv")})
        page = c.get("/contatos", params={"tag": "PT"}).text
        check("98111-4444" in page.replace(" ", "") or "981114444" in page.replace(" ", "").replace("-", ""),
              "filtro PT traz o contato PT")
        check("981112222" not in page.replace(" ", "").replace("-", ""), "filtro PT não traz contatos PTB")
        csv_out = c.get("/contatos/exportar.csv", params={"tag": "PT"}).text
        check("'=HYPERLINK" in csv_out, "exportação neutraliza fórmula do Excel")

        # id inválido no formulário de contato não dá erro 500
        tok = csrf(c.get("/contatos/novo").text)
        r = c.post("/contatos/salvar", data={"csrf_token": tok, "id": "999999", "name": "x"})
        check(r.status_code == 200 and "não encontrado" in r.text, "salvar contato inexistente não dá erro 500")
        tok = csrf(c.get("/contatos/novo").text)
        r = c.post("/contatos/salvar", data={"csrf_token": tok, "id": "abc"})
        check(r.status_code == 200, "id não numérico não dá erro 500")

        # mensagens longas não estouram o cookie de sessão
        tok = csrf(c.get("/configuracoes").text)
        files = [("files", (("doc_com_nome_longo_" * 8) + f"{i}.txt", b"x", "text/plain")) for i in range(30)]
        r = c.post("/configuracoes/conhecimento", data={"csrf_token": tok}, files=files)
        cookie = r.request.headers.get("cookie", "")
        check(len(c.cookies.get("marketero_session", "")) < 4000, f"cookie de sessão abaixo de 4 KB ({len(c.cookies.get('marketero_session', ''))})")
        check("Configurações" in c.get("/configuracoes").text, "usuário continua logado após mensagem longa")

        # webhook recusa corpo grande sem ler
        r = c.post("/webhook/whatsapp", content=b"x" * (1024 * 1024 + 10))
        check(r.status_code == 413, "webhook recusa corpo maior que 1 MB")


if __name__ == "__main__":
    init_db()
    asyncio.run(run_async_checks())
    web_checks()
    print("\n" + ("TUDO OK" if not failures else f"{len(failures)} FALHA(S): {failures}"))
    sys.exit(1 if failures else 0)
