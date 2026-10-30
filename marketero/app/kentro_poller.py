"""Leitura periódica do Kentro: mensagens recebidas, respostas do robô e situação dos disparos.

A API do Kentro documenta a consulta de atendimentos abertos (getAllOpenChats) e de mensagens
(getChatMessages); o robô responde por sendMessageToChat. Regras:
- só responde atendimento aguardando na fila (sem atendente) e fora da URA, salvo configuração;
- só responde mensagens recebidas depois que a integração foi ativada e há no máximo 30 minutos;
- mensagens enviadas por atendentes no Kentro também aparecem na conversa do Marketero.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import chatbot
from . import settings_store as cfg
from .db import db_session
from .kentro import (ENQUEUED_NO_WHATSAPP, ENQUEUED_STATUS, ENQUEUED_WAITING,
                     KentroClient, KentroError, to_seconds)
from .messaging import KentroChannel, get_channel
from .models import (CampaignRecipient, Conversation, KentroChat, Message,
                     utcnow)
from .phones import normalize

log = logging.getLogger("marketero.kentro")
REPLY_MAX_AGE = 30 * 60      # não responde mensagem recebida há mais de 30 min (ex.: sistema fora do ar)
STATUS_CHECK_EVERY = 30      # segundos entre verificações da situação dos disparos
STATUS = {"last_ok": None, "last_error": "", "open_chats": 0}


def describe_kentro_message(m: dict) -> tuple[str, str]:
    text = (m.get("text") or "").strip()
    f = m.get("file") or {}
    if f:
        mime = (f.get("mimeType") or "").lower()
        if mime.startswith("audio"):
            return "audio", "[O contato enviou um áudio, que você não consegue ouvir. Peça gentilmente que escreva.]"
        kind = "image" if mime.startswith("image") else "video" if mime.startswith("video") else "document"
        return kind, f"[O contato enviou um arquivo ({kind}){': ' + text if text else ''}]"
    if m.get("location"):
        loc = m["location"]
        return "location", f"[O contato enviou uma localização: {loc.get('latitude')}, {loc.get('longitude')}]"
    return "text", text


def integration_since(db: Session) -> float:
    """Momento em que a integração começou a ler o Kentro (persistido entre reinícios)."""
    raw = cfg.get(db, "kentro_since")
    if not raw:
        raw = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cfg.set_value(db, "kentro_since", raw)
        db.commit()
    return datetime.fromisoformat(raw).timestamp()


async def poll_chats(db: Session, client: KentroClient) -> list[int]:
    """Lê atendimentos abertos e mensagens novas. Devolve as conversas que o robô deve responder.

    Uma falha num atendimento (ex.: encerrado entre a listagem e a leitura) não interrompe os demais.
    """
    since = integration_since(db)
    chats = await client.open_chats()
    STATUS["open_chats"] = len(chats)
    open_ids, to_reply = set(), []
    for ch in chats:
        chat_id = int(ch.get("chatId") or 0)
        phone = normalize(str(ch.get("clientNumber") or ""))
        if not chat_id or not phone:
            continue
        open_ids.add(chat_id)
        try:
            conv_id = await _poll_one_chat(db, client, ch, chat_id, phone, since)
            if conv_id:
                to_reply.append(conv_id)
        except KentroError as e:
            db.rollback()
            log.info("Atendimento %s não lido neste ciclo: %s", chat_id, e)
        except Exception:
            db.rollback()
            log.exception("Erro ao ler o atendimento %s", chat_id)

    # Atendimentos encerrados no Kentro
    for conv in db.scalars(select(Conversation).where(Conversation.external_chat_id.is_not(None))).all():
        if conv.external_chat_id not in open_ids:
            conv.external_chat_id, conv.external_assigned = None, False
    for st in db.scalars(select(KentroChat).where(KentroChat.closed.is_(False))).all():
        if st.chat_id not in open_ids:
            st.closed = True
    db.commit()
    return [c for c in to_reply if c]


async def _poll_one_chat(db: Session, client: KentroClient, ch: dict, chat_id: int, phone: str,
                         since: float) -> int | None:
    """Lê as mensagens novas de um atendimento. Devolve o ID da conversa se o robô deve responder."""
    now = time.time()
    conv = chatbot.get_conversation(db, phone, ch.get("clientName") or "")
    state = db.get(KentroChat, chat_id)
    is_new_chat = state is None
    if is_new_chat:
        state = KentroChat(chat_id=chat_id, conversation_id=conv.id, last_kid=0)
        db.add(state)
        if conv.external_chat_id != chat_id and not conv.contact.opted_out:
            conv.bot_paused = False  # novo atendimento: o robô volta a atender
    conv.external_chat_id = chat_id
    conv.external_assigned = bool(ch.get("userId"))
    if conv.external_assigned:
        conv.needs_human = False  # um atendente assumiu no Kentro
    db.commit()

    messages, max_kid = await client.chat_messages(chat_id, state.last_kid or 0)
    reply_needed = False
    for m in sorted(messages, key=lambda x: int(x.get("kId") or 0)):
        kid = int(m.get("kId") or 0)
        if kid <= state.last_kid:
            continue
        state.last_kid = kid
        ext_id = f"k{kid}"
        if db.scalar(select(Message.id).where(Message.wa_message_id == ext_id)):
            continue  # já registrada (ex.: resposta enviada pelo próprio robô)
        ts = to_seconds(m.get("srvRcvTime") or m.get("messageTimestamp"))
        direction = m.get("direction")
        if direction == "in":
            kind, text = describe_kentro_message(m)
            received_at = datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None) if ts else None
            chatbot.register_inbound(db, conv, kind, text, ext_id, received_at)
            recent = ts >= since and now - ts <= REPLY_MAX_AGE if ts else True
            if recent and await chatbot.apply_optout(db, conv, kind, text):
                reply_needed = False
                continue
            reply_needed = reply_needed or recent
        elif direction == "out":
            body = (m.get("text") or "[arquivo]").strip()
            if not m.get("userId"):
                # Envio automático (template de campanha ou automação do Kentro): se a mensagem da
                # campanha já está registrada com o mesmo texto, só não a duplica.
                dup = db.scalar(select(Message.id).where(
                    Message.conversation_id == conv.id, Message.direction == "out", Message.body == body,
                    Message.created_at >= utcnow() - timedelta(days=2)))
                if dup:
                    continue
            db.add(Message(conversation_id=conv.id, direction="out", kind="text", status="sent",
                           author="human" if m.get("userId") else "campaign", body=body, wa_message_id=ext_id))
            conv.last_message_at = utcnow()
    if not messages and max_kid:
        state.last_kid = max(state.last_kid, max_kid)
    db.commit()
    return conv.id if reply_needed and not ch.get("onIvr") else None


# Rodízio na verificação dos disparos: cada grupo avança um cursor, para que envios que ficam
# muito tempo "entregues" não monopolizem as consultas e todos sejam atualizados.
_status_cursor = {"queued": 0, "sent": 0}
STATUS_BATCH = {"queued": 25, "sent": 15}


def _next_batch(db: Session, group: str, statuses: tuple[str, ...]) -> list[CampaignRecipient]:
    since = utcnow() - timedelta(days=3)
    base = select(CampaignRecipient).where(CampaignRecipient.wa_message_id.like("q%"),
                                           CampaignRecipient.status.in_(statuses),
                                           CampaignRecipient.sent_at >= since)
    recs = db.scalars(base.where(CampaignRecipient.id > _status_cursor[group])
                      .order_by(CampaignRecipient.id).limit(STATUS_BATCH[group])).all()
    if len(recs) < STATUS_BATCH[group]:
        _status_cursor[group] = 0  # chegou ao fim: o próximo ciclo recomeça do início
    else:
        _status_cursor[group] = recs[-1].id
    return recs


async def check_enqueued(db: Session, client: KentroClient) -> None:
    """Atualiza a situação dos disparos enfileirados no Kentro (enviado, entregue, lido, falha)."""
    recs = _next_batch(db, "queued", ("queued",)) + _next_batch(db, "sent", ("sent", "delivered"))
    for rec in recs:
        try:
            data = await client.check_enqueued(int(rec.wa_message_id[1:]))
        except KentroError as e:
            log.info("checkEnqueuedMessage %s: %s", rec.wa_message_id, e)
            continue
        code = int(data.get("status") or 0)
        msg = db.scalar(select(Message).where(Message.wa_message_id == rec.wa_message_id))
        if code in ENQUEUED_WAITING:
            new = "queued"
        elif code == 1:
            new = "read" if data.get("clientReadTime") else "delivered" if data.get("clientRcvTime") else "sent"
        else:
            new = "failed"
            rec.error = ENQUEUED_STATUS.get(code, f"falha (código {code})")
            if code in ENQUEUED_NO_WHATSAPP:
                rec.contact.whatsapp_status = "invalid"
        order = {"queued": 0, "sent": 1, "delivered": 2, "read": 3}
        if new == "failed" or order.get(new, 0) > order.get(rec.status, 0):
            rec.status = new
            if msg:
                msg.status = new if new != "queued" else msg.status
                msg.error = rec.error if new == "failed" else msg.error
        if new in ("delivered", "read"):
            rec.contact.whatsapp_status = "valid"
    db.commit()


# Respostas da IA rodam fora do laço de leitura (uma IA lenta não atrasa a leitura dos atendimentos),
# no máximo REPLY_CONCURRENCY ao mesmo tempo e uma por conversa.
REPLY_CONCURRENCY = 3
_reply_sem = asyncio.Semaphore(REPLY_CONCURRENCY)
_inflight: set[int] = set()
_again: set[int] = set()
_tasks: set[asyncio.Task] = set()


async def _wait_reply_delay(conv_id: int) -> None:
    """Espera o tempo de resposta configurado, contado da última mensagem do contato.

    Confere de novo a cada poucos segundos: mensagem nova no meio do caminho reinicia a contagem.
    """
    while True:
        with db_session() as db:
            conv = db.get(Conversation, conv_id)
            if not conv or not conv.last_inbound_at:
                return
            due = conv.last_inbound_at + timedelta(seconds=chatbot.reply_delay(db, conv))
        remaining = (due - utcnow()).total_seconds()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, 5))


async def _reply(conv_id: int) -> None:
    try:
        while True:
            await _wait_reply_delay(conv_id)
            _again.discard(conv_id)
            async with _reply_sem, chatbot.conversation_lock(conv_id):
                with db_session() as db:
                    conv = db.get(Conversation, conv_id)
                    if conv:
                        await chatbot.reply_with_ai(db, conv)
            if conv_id not in _again:  # chegou mensagem nova durante a resposta: responde de novo
                break
    except Exception:
        log.exception("Erro ao responder a conversa %s", conv_id)
    finally:
        _inflight.discard(conv_id)


def schedule_reply(conv_id: int) -> None:
    if conv_id in _inflight:
        _again.add(conv_id)
        return
    _inflight.add(conv_id)
    task = asyncio.create_task(_reply(conv_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def run(stop: asyncio.Event) -> None:
    log.info("Leitor do Kentro iniciado")
    last_status_check = 0.0
    while not stop.is_set():
        interval = 8
        try:
            with db_session() as db:
                interval = max(3, cfg.get_int(db, "kentro_poll_seconds", 8))
                channel = get_channel(db)
                if isinstance(channel, KentroChannel) and channel.configured:
                    for conv_id in await poll_chats(db, channel.client):
                        schedule_reply(conv_id)
                    if time.time() - last_status_check >= STATUS_CHECK_EVERY:
                        await check_enqueued(db, channel.client)
                        last_status_check = time.time()
                    STATUS["last_ok"], STATUS["last_error"] = utcnow(), ""
        except KentroError as e:
            if STATUS["last_error"] != str(e):
                log.warning("Kentro: %s", e)
            STATUS["last_error"] = str(e)
        except Exception as e:  # o laço não pode morrer por um erro pontual
            log.exception("Erro no leitor do Kentro")
            STATUS["last_error"] = f"erro interno: {e}"
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass
