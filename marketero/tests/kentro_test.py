"""Teste da integração com o Kentro contra um servidor falso que segue a especificação da API.

  MARKETERO_DATA_DIR=$(mktemp -d) MARKETERO_SECRET_KEY=t python -m tests.kentro_test
"""
import asyncio
import json
import sys
import time

import httpx

from app import ai, chatbot, dispatcher, kentro, kentro_poller
from app import settings_store as cfg
from app.db import db_session, init_db
from app.messaging import MessagingError, get_channel
from app.models import (Campaign, CampaignRecipient, Contact, Conversation,
                        Message)

failures = []


def check(cond, label):
    print(("OK   " if cond else "FALHA"), label)
    if not cond:
        failures.append(label)


async def fake_reply(conf, system, history, system_dynamic=""):
    last = history[-1]["content"]
    if "proposta" in last.lower():
        return "Ótimo! Vou chamar a equipe comercial. [[HUMANO]]"
    return f"Resposta da IA: {last}"


ai.generate_reply = fake_reply
ai.AIConfig.configured = property(lambda self: True)

NOW = time.time()


class FakeKentro:
    def __init__(self):
        self.chats = []
        self.messages: dict[int, list] = {}
        self.enqueued = {}
        self.sent = []  # (chatId, text, info)
        self.next_kid = 1000
        self.failing_chats = set()
        self.checked = {}  # enqueuedId -> vezes consultado

    def add_msg(self, chat_id, direction, text, ts=None, user_id=0):
        self.next_kid += 1
        self.messages.setdefault(chat_id, []).append({
            "kId": self.next_kid, "direction": direction, "text": text, "userId": user_id,
            "srvRcvTime": int((ts or time.time()) * 1000)})
        return self.next_kid

    def handler(self, request: httpx.Request) -> httpx.Response:
        op = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content or b"{}")
        if body.get("apiKey") != "chave" or body.get("queueId") != 7:
            return httpx.Response(401, json={"errorCode": "AUTH_018"})
        if op == "getQueueStatus":
            return httpx.Response(200, json={"name": "WhatsApp Oficial", "type": "WAO", "connected": True,
                                             "authenticated": True, "authenticatedNumber": "553899990000",
                                             "openChats": len(self.chats), "loggedAgentsCount": 2})
        if op == "getTemplates":
            return httpx.Response(200, json={"total": 2, "page": 1, "limit": 500, "templates": [
                {"id": 42, "name": "apresentacao", "type": "meta", "status": "APPROVED", "language": "pt_BR",
                 "category": "MARKETING", "text": "Olá {{1}}, do {{2}}!", "totalParams": 2, "headerType": "none"},
                {"id": 43, "name": "rejeitado", "type": "meta", "status": "REJECTED", "language": "pt_BR",
                 "text": "x", "totalParams": 0}]})
        if op == "enqueueMessageToSend":
            eid = len(self.enqueued) + 1
            self.enqueued[eid] = body
            return httpx.Response(200, json={"enqueuedId": eid})
        if op == "checkEnqueuedMessage":
            eid = body["enqueuedId"]
            self.checked[eid] = self.checked.get(eid, 0) + 1
            if eid >= 1000:  # disparos do teste de rodízio: entregues e nunca lidos
                return httpx.Response(200, json={"status": 1, "kId": 1, "clientRcvTime": int(NOW * 1000)})
            return httpx.Response(200, json={"status": 1, "kId": 1, "clientRcvTime": int(NOW * 1000),
                                             "clientReadTime": int(NOW * 1000)})
        if op == "getAllOpenChats":
            return httpx.Response(200, json={"openChats": len(self.chats), "chats": self.chats})
        if op == "getChatMessages":
            if body["chatId"] in self.failing_chats:
                return httpx.Response(500, json={"message": "erro interno simulado"})
            msgs = [m for m in self.messages.get(body["chatId"], []) if m["kId"] >= body.get("fromId", 0)]
            return httpx.Response(200, json={"messages": msgs, "maxKId": max([m["kId"] for m in msgs], default=0)})
        if op == "sendMessageToChat":
            self.sent.append((body["chatId"], body["text"], body.get("info", False)))
            kid = self.add_msg(body["chatId"], "info" if body.get("info") else "out", body["text"])
            return httpx.Response(200, json={"kId": kid, "mId": f"wamid.{kid}"})
        return httpx.Response(404, json={"message": f"op {op} não simulada"})


fake = FakeKentro()
kentro.TRANSPORT = httpx.MockTransport(fake.handler)


def chat(chat_id, number, name="", user_id=0, on_ivr=False):
    return {"chatId": chat_id, "clientNumber": number, "clientName": name, "userId": user_id, "onIvr": on_ivr,
            "status": 1}


async def poll():
    with db_session() as db:
        to_reply = await kentro_poller.poll_chats(db, get_channel(db).client)
    for cid in to_reply:
        with db_session() as db:
            await chatbot.reply_with_ai(db, db.get(Conversation, cid))


def conv_of(db, phone):
    return db.query(Conversation).join(Contact).filter(Contact.phone == phone).one()


async def main():
    init_db()
    with db_session() as db:
        for k, v in {"wa_provider": "kentro", "kentro_base_url": "https://kentro.teste", "kentro_queue_id": "7",
                     "kentro_api_key": "errada", "wa_simulation": "0", "bot_enabled": "1",
                     "dispatch_hour_start": "0", "dispatch_hour_end": "24", "dispatch_weekends": "1"}.items():
            cfg.set_value(db, k, v)
        db.commit()
        try:
            await get_channel(db).test()
            check(False, "chave errada é recusada")
        except MessagingError as e:
            check("AUTH_018" in str(e) and "inválida" in str(e), f"chave errada é recusada ({e})")
        cfg.set_value(db, "kentro_api_key", "chave")
        db.commit()
        msg = await get_channel(db).test()
        check("WhatsApp Oficial" in msg and "1 template" in msg, f"teste de conexão ({msg})")
        tpls = await get_channel(db).list_templates()
        check([t["id"] for t in tpls] == ["42"] and tpls[0]["params"] == 2, "templates aprovados (rejeitado fica de fora)")

        # --- campanha ---
        maria = Contact(name="Maria Souza", organization="PXX", phone="5538999991111", tags="teste")
        db.add(maria)
        db.flush()
        camp = Campaign(name="Teste Kentro", template_name="apresentacao", template_ref="42", template_language="pt_BR",
                        template_body="Olá {{1}}, do {{2}}!", variables="{nome}\n{organizacao}", status="running",
                        rate_per_minute=12)
        db.add(camp)
        db.flush()
        db.add(CampaignRecipient(campaign_id=camp.id, contact_id=maria.id))
        db.commit()
        camp_id = camp.id

    await dispatcher.tick()
    sent = fake.enqueued.get(1, {})
    check(sent.get("number") == "38999991111" and sent.get("country") == "BR", "número enviado sem o 55 e com country BR")
    check(sent.get("templateId") == 42 and sent.get("varsdata") == ["Maria", "PXX"], "template e variáveis do envio")
    check(sent.get("campaignName") == "Teste Kentro" and sent.get("actionwhenopen") == 1, "nome da campanha e regra de atendimento aberto")
    with db_session() as db:
        rec = db.query(CampaignRecipient).filter_by(campaign_id=camp_id).one()
        check(rec.status == "queued" and rec.wa_message_id == "q1", "destinatário fica 'na fila do Kentro'")
        await kentro_poller.check_enqueued(db, get_channel(db).client)
        db.refresh(rec)
        check(rec.status == "read", "situação do disparo atualizada até 'lida'")

    # --- conversa antiga (antes da ativação): registra, não responde ---
    fake.chats.append(chat(100, "5538988880000", "Antigo"))
    fake.add_msg(100, "in", "mensagem de ontem", ts=NOW - 86400)
    await poll()
    check(not [s for s in fake.sent if s[0] == 100], "mensagem antiga não é respondida")

    # --- resposta da Maria à campanha ---
    fake.chats.append(chat(101, "5538999991111", "Maria"))
    fake.add_msg(101, "in", "Oi, que serviços vocês têm?")
    await poll()
    check(any(s[0] == 101 and "Resposta da IA" in s[1] for s in fake.sent), "robô responde pelo atendimento (sendMessageToChat)")
    await poll()
    with db_session() as db:
        conv = conv_of(db, "5538999991111")
        bot_msgs = [m for m in conv.messages if m.author == "bot" and "Resposta da IA" in m.body]
        check(len(bot_msgs) == 1, "resposta do robô não é duplicada na leitura seguinte")
        check(conv.external_chat_id == 101 and conv.campaign_id == camp_id, "conversa ligada ao atendimento e à campanha")
        check(db.query(CampaignRecipient).filter_by(campaign_id=camp_id).one().status == "replied", "campanha registra a resposta")

    # --- atendente humano assumiu: robô não responde, mas a mensagem do atendente aparece ---
    fake.chats.append(chat(102, "5538977770000", "Carlos", user_id=5))
    fake.add_msg(102, "in", "Bom dia")
    fake.add_msg(102, "out", "Bom dia Carlos, aqui é a Ana", user_id=5)
    n_before = len(fake.sent)
    await poll()
    check(len(fake.sent) == n_before, "atendimento com atendente: robô não responde")
    with db_session() as db:
        check(any(m.author == "human" and "Ana" in m.body for m in conv_of(db, "5538977770000").messages),
              "mensagem do atendente aparece na conversa")

    # --- pedido de proposta: sinaliza para a equipe com nota interna, sem pausar o robô ---
    fake.add_msg(101, "in", "Quero uma proposta")
    await poll()
    check(any(s[0] == 101 and s[2] and "atendimento humano" in s[1] for s in fake.sent), "nota interna para os atendentes")
    with db_session() as db:
        cv = conv_of(db, "5538999991111")
        check(cv.needs_human and not cv.bot_paused, "pedido de proposta sinaliza a equipe e o robô segue ativo")

    # --- descadastro ---
    fake.chats.append(chat(103, "5538966660000", "Pedro"))
    fake.add_msg(103, "in", "SAIR")
    await poll()
    with db_session() as db:
        check(conv_of(db, "5538966660000").contact.opted_out, "descadastro por palavra-chave")
    check(any(s[0] == 103 and "não receberá mais" in s[1] for s in fake.sent), "confirmação de descadastro enviada")

    # --- erro num atendimento não impede os outros de serem respondidos ---
    fake.chats.append(chat(104, "5538955550000", "Com erro"))
    fake.failing_chats.add(104)
    fake.chats.append(chat(105, "5538944440000", "Lucas"))
    fake.add_msg(105, "in", "Olá, tudo bem?")
    await poll()
    check(any(s[0] == 105 for s in fake.sent), "atendimento com erro não impede a resposta de outro")
    fake.failing_chats.clear()

    # --- rodízio na verificação dos disparos (envios que ficam "entregues" não monopolizam) ---
    with db_session() as db:
        camp2 = Campaign(name="Rodízio", template_name="t", template_ref="42", status="finished")
        db.add(camp2)
        db.flush()
        for i in range(60):
            c = Contact(name=f"C{i}", phone=f"55389700{i:05d}")
            db.add(c)
            db.flush()
            db.add(CampaignRecipient(campaign_id=camp2.id, contact_id=c.id, status="queued",
                                     wa_message_id=f"q{1000 + i}", sent_at=__import__("app.models").models.utcnow()))
        db.commit()
        for _ in range(6):
            await kentro_poller.check_enqueued(db, get_channel(db).client)
    unchecked = [eid for eid in range(1000, 1060) if not fake.checked.get(eid)]
    check(not unchecked, f"todos os 60 disparos foram verificados em rodízio (faltaram {len(unchecked)})")

    # --- atendimento encerrado ---
    fake.chats = [c for c in fake.chats if c["chatId"] != 101]
    await poll()
    with db_session() as db:
        check(conv_of(db, "5538999991111").external_chat_id is None, "atendimento encerrado é desvinculado")
        conv = conv_of(db, "5538999991111")
        m = await chatbot.send_text(db, conv, "teste", author="human")
        check(m.status == "failed" and "atendimento aberto" in m.error, "sem atendimento aberto: envio manual explica o motivo")

    print("\n" + ("TUDO OK" if not failures else f"{len(failures)} FALHA(S): {failures}"))
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    asyncio.run(main())
