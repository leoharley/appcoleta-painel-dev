"""Tratamento das mensagens recebidas e respostas automáticas."""
import asyncio
import logging
import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import ai
from . import settings_store as cfg
from .db import db_session
from .models import (Campaign, CampaignRecipient, Contact, Conversation,
                     KnowledgeDoc, Message, utcnow)
from .messaging import KentroChannel, MessagingError, get_channel
from .phones import is_mobile, normalize
from .whatsapp import ERR_NOT_ON_WHATSAPP, ERR_USER_STOPPED_MARKETING

log = logging.getLogger("marketero.chatbot")

HANDOFF_MARK = "[[HUMANO]]"
WINDOW = timedelta(hours=24)

_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def conversation_lock(conv_id: int) -> asyncio.Lock:
    """Trava por conversa (evita duas respostas da IA ao mesmo tempo); descarta as ociosas de vez em quando."""
    if len(_locks) > 2000:
        for key in [k for k, lock in _locks.items() if not lock.locked()]:
            del _locks[key]
    return _locks[conv_id]

RULES = f"""
Regras de formato e conduta:
- Você está conversando pelo WhatsApp: mensagens curtas, linguagem natural, no máximo um emoji, sem markdown com títulos ou tabelas.
- Responda sempre em português do Brasil.
- Se a pessoa pedir para falar com uma pessoa da equipe, demonstrar interesse em contratar, pedir proposta ou orçamento, ou se você não souber responder com segurança, diga que a equipe foi avisada e vai entrar em contato, continue ajudando no que puder e escreva {HANDOFF_MARK} no fim da mensagem.
- Se a pessoa disser que não quer receber mensagens, respeite: agradeça e informe que ela pode enviar SAIR para não ser mais contatada.
"""


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def is_optout(db: Session, text: str) -> bool:
    words = [_strip_accents(w.strip()).upper() for w in cfg.get(db, "optout_keywords").split(",") if w.strip()]
    msg = _strip_accents(re.sub(r"[^\w\s]", "", text or "")).strip().upper()
    return msg in words


def canonical_phone(wa_id: str) -> str:
    """wa_id do WhatsApp na forma usada pelos contatos (com o 9 dos celulares brasileiros)."""
    return normalize("+" + wa_id) or wa_id


def get_conversation(db: Session, wa_id: str, profile_name: str = "") -> Conversation:
    wa_id = canonical_phone(wa_id)
    contact = db.scalar(select(Contact).where(Contact.phone == wa_id))
    if contact is None:
        contact = Contact(phone=wa_id, phone_is_mobile=is_mobile(wa_id), name=profile_name or "",
                          source="whatsapp", tags="whatsapp")
        db.add(contact)
        db.flush()
    elif profile_name and not contact.name:
        contact.name = profile_name
    conv = db.scalar(select(Conversation).where(Conversation.contact_id == contact.id))
    if conv is None:
        conv = Conversation(contact_id=contact.id)
        db.add(conv)
        db.flush()
    return conv


def window_open(conv: Conversation) -> bool:
    return bool(conv.last_inbound_at and utcnow() - conv.last_inbound_at < WINDOW)


async def send_text(db: Session, conv: Conversation, text: str, author: str) -> Message:
    """Envia texto livre (só dentro da janela de 24 h) pelo canal configurado e registra na conversa."""
    channel = get_channel(db)
    msg = Message(conversation_id=conv.id, direction="out", author=author, kind="text", body=text)
    try:
        if not window_open(conv):
            raise MessagingError("Fora da janela de 24 h: o contato precisa responder antes, ou use um template.")
        msg.wa_message_id = await channel.send_text(conv, text)
        msg.status = "simulated" if channel.simulation else "sent"
    except MessagingError as e:
        msg.status, msg.error = "failed", str(e)
    db.add(msg)
    conv.last_message_at = utcnow()
    db.commit()
    return msg


def build_static_prompt(db: Session) -> str:
    """Parte fixa das instruções (igual em todas as conversas, aproveitada pelo cache de prompt)."""
    parts = [cfg.get(db, "system_prompt").strip()]
    kb = cfg.get(db, "knowledge_base").strip()
    if kb:
        parts.append("Informações que você pode usar:\n" + kb)
    docs = db.scalars(select(KnowledgeDoc).where(KnowledgeDoc.enabled.is_(True)).order_by(KnowledgeDoc.name)).all()
    if docs:
        parts.append("Base de conhecimento adicional (documentos de referência; use-os para responder, "
                     "sem citar os nomes dos arquivos):\n\n" + "\n\n".join(
                         f'<documento nome="{d.name}">\n{d.content.strip()}\n</documento>' for d in docs))
    parts.append(RULES.strip())
    return "\n\n".join(p for p in parts if p)


def build_dynamic_prompt(db: Session, conv: Conversation) -> str:
    """Parte que muda por conversa: dados do contato e da campanha de origem."""
    parts = []
    c = conv.contact
    who = ", ".join(x for x in [c.name, c.role, c.organization, f"{c.city}/{c.uf}" if c.city else ""] if x)
    if who:
        parts.append(f"Dados que temos do contato: {who}.")
    if conv.campaign_id:
        camp = db.get(Campaign, conv.campaign_id)
        if camp:
            first = db.scalar(select(Message).where(Message.conversation_id == conv.id, Message.author == "campaign")
                              .order_by(Message.id.desc()))
            ctx = f"Esta conversa começou com uma mensagem nossa da campanha \"{camp.name}\""
            if first and first.body:
                ctx += f":\n\"{first.body}\""
            parts.append(ctx + ".")
            if camp.ai_context.strip():
                parts.append("Orientações desta campanha:\n" + camp.ai_context.strip())
    return "\n\n".join(p for p in parts if p)


def history_for_ai(db: Session, conv: Conversation) -> list[dict]:
    limit = max(4, cfg.get_int(db, "history_messages", 20))
    rows = db.scalars(select(Message).where(Message.conversation_id == conv.id, Message.status != "failed")
                      .order_by(Message.id.desc()).limit(limit)).all()
    hist = []
    for m in reversed(rows):
        if m.author == "campaign" or m.kind == "system":
            continue  # campanha entra no prompt como contexto; notas internas não são falas
        hist.append({"role": "user" if m.direction == "in" else "assistant", "content": m.body})
    return hist


def describe_incoming(msg: dict) -> tuple[str, str]:
    """Converte a mensagem do webhook em (tipo, texto)."""
    t = msg.get("type", "")
    if t == "text":
        return "text", msg.get("text", {}).get("body", "")
    if t == "button":
        return "button", msg.get("button", {}).get("text", "")
    if t == "interactive":
        it = msg.get("interactive", {})
        reply = it.get("button_reply") or it.get("list_reply") or {}
        return "interactive", reply.get("title", "")
    if t in ("image", "video", "document", "sticker"):
        caption = msg.get(t, {}).get("caption", "")
        return t, f"[O contato enviou um(a) {t}{': ' + caption if caption else ''}]"
    if t == "audio":
        return "audio", "[O contato enviou um áudio, que você não consegue ouvir. Peça gentilmente que escreva.]"
    if t == "location":
        loc = msg.get("location", {})
        return "location", f"[O contato enviou uma localização: {loc.get('name') or ''} {loc.get('address') or ''}]".strip()
    return t or "unknown", f"[Mensagem do tipo {t} recebida]"


def register_inbound(db: Session, conv: Conversation, kind: str, text: str, external_id: str | None,
                     received_at: datetime | None = None) -> Message:
    """Grava uma mensagem recebida e atualiza conversa, contato e campanha de origem.

    received_at: hora real da mensagem (histórico lido do Kentro); define a janela de 24 h.
    """
    contact = conv.contact
    now = utcnow()
    when = min(received_at, now) if received_at else now
    msg = Message(conversation_id=conv.id, direction="in", author="contact", kind=kind, body=text,
                  wa_message_id=external_id, status="received", created_at=when)
    db.add(msg)
    if not conv.last_inbound_at or when > conv.last_inbound_at:
        conv.last_inbound_at = when
    conv.last_message_at = max(conv.last_message_at or when, when)
    if now - when < timedelta(days=1):
        conv.unread += 1
    contact.whatsapp_status = "valid"
    if conv.campaign_id:
        rec = db.scalar(select(CampaignRecipient).where(CampaignRecipient.campaign_id == conv.campaign_id,
                                                       CampaignRecipient.contact_id == contact.id))
        if rec and rec.status in ("queued", "sent", "delivered", "read"):
            rec.status = "replied"
    db.commit()
    return msg


async def apply_optout(db: Session, conv: Conversation, kind: str, text: str) -> bool:
    """Se a mensagem for uma palavra de descadastro, descadastra e confirma. Devolve True nesse caso."""
    if kind not in ("text", "button", "interactive") or not is_optout(db, text):
        return False
    conv.contact.opted_out, conv.contact.opted_out_at = True, utcnow()
    conv.bot_paused = True
    db.commit()
    await send_text(db, conv, cfg.get(db, "optout_reply"), author="bot")
    return True


def reply_delay(db: Session, conv: Conversation) -> int:
    """Segundos de espera depois da última mensagem do contato antes de responder.

    Mensagens que chegam nesse intervalo são respondidas juntas. "Primeira resposta" = o chatbot (ou a
    equipe) ainda não respondeu esta pessoa nas últimas 24 h.
    """
    last_reply = db.scalar(select(Message.created_at).where(
        Message.conversation_id == conv.id, Message.direction == "out", Message.author.in_(("bot", "human")),
        Message.kind != "system").order_by(Message.id.desc()))
    first = last_reply is None or utcnow() - last_reply > WINDOW
    key = "reply_delay_first" if first else "reply_delay_next"
    return min(max(cfg.get_int(db, key, 4), 0), cfg.MAX_REPLY_DELAY)


async def handle_inbound(wa_id: str, profile_name: str, raw_msg: dict) -> None:
    """Mensagem recebida pelo webhook da Cloud API da Meta."""
    kind, text = describe_incoming(raw_msg)
    wa_msg_id = raw_msg.get("id")
    with db_session() as db:
        if wa_msg_id and db.scalar(select(Message.id).where(Message.wa_message_id == wa_msg_id)):
            return  # notificação repetida pela Meta
        conv = get_conversation(db, wa_id, profile_name)
        msg = register_inbound(db, conv, kind, text, wa_msg_id)
        conv_id, msg_id = conv.id, msg.id
        if wa_msg_id:
            await get_channel(db).mark_read(wa_msg_id)
        if await apply_optout(db, conv, kind, text):
            return
        delay = reply_delay(db, conv)

    await asyncio.sleep(delay)
    async with conversation_lock(conv_id):
        with db_session() as db:
            conv = db.get(Conversation, conv_id)
            last_in = db.scalar(select(Message.id).where(Message.conversation_id == conv_id, Message.direction == "in")
                                .order_by(Message.id.desc()))
            if last_in != msg_id:
                return  # chegou mensagem mais nova; ela responde pelas duas
            await reply_with_ai(db, conv)


async def reply_with_ai(db: Session, conv: Conversation) -> Message | None:
    if conv.bot_paused or conv.contact.opted_out or not cfg.get_bool(db, "bot_enabled"):
        return None
    channel = get_channel(db)
    if isinstance(channel, KentroChannel) and conv.external_assigned and not cfg.get_bool(db, "kentro_answer_assigned"):
        return None  # um atendente humano já assumiu o atendimento no Kentro
    conf = ai.config_from_settings(db)
    if not conf.configured:
        log.warning("IA não configurada; conversa %s sem resposta automática", conv.id)
        return None
    try:
        reply = await ai.generate_reply(conf, build_static_prompt(db), history_for_ai(db, conv),
                                        build_dynamic_prompt(db, conv))
    except ai.AIError as e:
        log.error("Falha da IA na conversa %s: %s", conv.id, e)
        db.add(Message(conversation_id=conv.id, direction="out", author="bot", kind="system",
                       body=f"[A IA não respondeu: {e}]", status="failed", error=str(e)))
        db.commit()
        return None
    # A geração leva segundos: se nesse meio-tempo alguém assumiu a conversa, a resposta é descartada.
    db.refresh(conv)
    db.refresh(conv.contact)
    if conv.bot_paused or conv.contact.opted_out or (
            isinstance(channel, KentroChannel) and conv.external_assigned
            and not cfg.get_bool(db, "kentro_answer_assigned")):
        log.info("Resposta da IA descartada: conversa %s foi assumida durante a geração", conv.id)
        return None
    handoff = HANDOFF_MARK in reply
    reply = reply.replace(HANDOFF_MARK, "").strip()
    msg = await send_text(db, conv, reply, author="bot")
    # Só sinaliza: quem pausa o robô e assume a conversa é a equipe, manualmente.
    if handoff and not conv.needs_human:
        conv.needs_human = True
        db.add(Message(conversation_id=conv.id, direction="out", author="bot", kind="system", status="sent",
                       body="[Precisa de atendimento humano: o chatbot continua respondendo até alguém assumir]"))
        db.commit()
        if isinstance(channel, KentroChannel):
            await channel.send_note(conv, "🤖 Marketero: o cliente pediu atendimento humano ou demonstrou interesse "
                                          "comercial. O robô continua respondendo até um atendente assumir.")
    return msg


STATUS_ORDER = {"sent": 1, "delivered": 2, "read": 3}


def handle_status(st: dict) -> None:
    """Atualiza entregas/leituras/falhas a partir do webhook."""
    wamid, status = st.get("id"), st.get("status")
    if not wamid or not status:
        return
    errors = st.get("errors") or []
    err_txt = "; ".join(f"{e.get('title') or e.get('message')} ({e.get('code')})" for e in errors)
    codes = {e.get("code") for e in errors}
    with db_session() as db:
        for m in db.scalars(select(Message).where(Message.wa_message_id == wamid)).all():
            if status == "failed":
                m.status, m.error = "failed", err_txt
            elif STATUS_ORDER.get(status, 0) > STATUS_ORDER.get(m.status, 0):
                m.status = status
        rec = db.scalar(select(CampaignRecipient).where(CampaignRecipient.wa_message_id == wamid))
        if rec:
            if status == "failed":
                rec.status, rec.error = "failed", err_txt
            elif rec.status != "replied" and STATUS_ORDER.get(status, 0) > STATUS_ORDER.get(rec.status, 0):
                rec.status = status
        recipient = st.get("recipient_id")
        contact = db.scalar(select(Contact).where(Contact.phone == canonical_phone(recipient))) if recipient else None
        if contact:
            if codes & ERR_NOT_ON_WHATSAPP:
                contact.whatsapp_status = "invalid"
            elif status in ("delivered", "read"):
                contact.whatsapp_status = "valid"
            if codes & ERR_USER_STOPPED_MARKETING:
                contact.opted_out, contact.opted_out_at = True, utcnow()
        db.commit()
