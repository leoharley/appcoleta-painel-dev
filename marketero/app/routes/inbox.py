"""Caixa de entrada: conversas do WhatsApp, resposta manual e pausa do chatbot."""
import hashlib

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import chatbot
from ..db import get_db
from ..models import Conversation, Message
from ..security import check_csrf, require_login
from ..web import flash, redirect, render

router = APIRouter(prefix="/conversas")


@router.get("")
async def list_conversations(request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    # As que precisam de alguém da equipe ficam no topo.
    convs = db.scalars(select(Conversation).order_by(Conversation.needs_human.desc(),
                                                     Conversation.last_message_at.desc().nulls_last()).limit(200)).all()
    last = {}
    for c in convs:
        last[c.id] = db.scalar(select(Message).where(Message.conversation_id == c.id).order_by(Message.id.desc()))
    return render(request, "inbox.html", conversations=convs, last=last)


@router.get("/estado")
async def state(request: Request, cid: int = 0, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    """Assinatura barata do que está na tela; a página só é buscada de novo quando ela muda."""
    if cid:
        conv = db.get(Conversation, cid)
        rows = db.execute(select(Message.id, Message.status, Message.error).where(Message.conversation_id == cid)
                          .order_by(Message.id.desc()).limit(30)).all()
        stamp = (f"{[tuple(r) for r in rows]}|{conv and conv.bot_paused}|{conv and conv.needs_human}"
                 f"|{conv and conv.contact.opted_out}")
    else:
        stamp = (f"{db.scalar(select(func.max(Message.id)))}|{db.scalar(select(func.sum(Conversation.unread)))}"
                 f"|{db.scalar(select(func.count()).where(Conversation.needs_human.is_(True)))}")
    return {"v": hashlib.sha1(stamp.encode()).hexdigest()}


@router.get("/{cid:int}")
async def view_conversation(cid: int, request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    conv = db.get(Conversation, cid)
    if not conv:
        return redirect("/conversas")
    if conv.unread:
        conv.unread = 0
        db.commit()
    return render(request, "conversation.html", conv=conv, messages=conv.messages[-300:],
                  window_open=chatbot.window_open(conv))


@router.post("/{cid:int}/enviar")
async def send(cid: int, request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    form = await check_csrf(request)
    conv = db.get(Conversation, cid)
    text = str(form.get("text", "")).strip()
    if conv and text:
        msg = await chatbot.send_text(db, conv, text, author="human")
        if msg.status == "failed":
            flash(request, f"Não enviada: {msg.error}", "danger")
        else:
            conv.needs_human = False  # a equipe respondeu
            if form.get("pause_bot"):
                conv.bot_paused = True
            db.commit()
    return redirect(f"/conversas/{cid}")


@router.post("/{cid:int}/bot")
async def toggle_bot(cid: int, request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    form = await check_csrf(request)
    conv = db.get(Conversation, cid)
    if conv:
        if form.get("action") == "resolve":
            conv.needs_human = False
            db.commit()
            flash(request, "Sinalização de atendimento humano removida.")
            return redirect(f"/conversas/{cid}")
        conv.bot_paused = form.get("action") == "pause"
        if conv.bot_paused:
            conv.needs_human = False  # alguém da equipe assumiu
        db.commit()
        if form.get("action") == "resume_reply":
            async with chatbot.conversation_lock(conv.id):
                reply = await chatbot.reply_with_ai(db, conv)
            if reply is None:
                flash(request, "O chatbot não respondeu (veja se a IA está configurada e o contato não está descadastrado).",
                      "warning")
        flash(request, "Chatbot pausado nesta conversa." if conv.bot_paused else "Chatbot ativo nesta conversa.")
    return redirect(f"/conversas/{cid}")
