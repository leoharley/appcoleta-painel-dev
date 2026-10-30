"""Webhook da WhatsApp Cloud API (configurado no painel da Meta).

GET  -> verificação inicial (hub.challenge)
POST -> mensagens recebidas e status de entrega, com assinatura X-Hub-Signature-256 validada.
"""
import asyncio
import json
import logging

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, Response

from .. import chatbot
from .. import settings_store as cfg
from ..db import db_session
from ..models import utcnow
from ..whatsapp import verify_signature

log = logging.getLogger("marketero.webhook")
router = APIRouter()
_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


@router.get("/webhook/whatsapp")
async def verify(request: Request):
    q = request.query_params
    with db_session() as db:
        expected = cfg.get(db, "wa_verify_token")
    if q.get("hub.mode") == "subscribe" and expected and q.get("hub.verify_token") == expected:
        with db_session() as db:
            cfg.set_value(db, "wa_webhook_verified", "1")
            db.commit()
        return PlainTextResponse(q.get("hub.challenge", ""))
    return Response(status_code=403)


MAX_WEBHOOK_BYTES = 1024 * 1024


@router.post("/webhook/whatsapp")
async def receive(request: Request):
    # Endpoint público: recusa corpos grandes antes de ler (as notificações da Meta têm poucos KB).
    try:
        if int(request.headers.get("content-length") or 0) > MAX_WEBHOOK_BYTES:
            return Response(status_code=413)
    except ValueError:
        return Response(status_code=400)
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_WEBHOOK_BYTES:
            return Response(status_code=413)
    with db_session() as db:
        secret = cfg.get(db, "wa_app_secret")
    if not verify_signature(secret, raw, request.headers.get("x-hub-signature-256")):
        log.warning("Webhook com assinatura inválida ou App Secret não configurado")
        return Response(status_code=401)
    try:
        payload = json.loads(raw)
    except ValueError:
        return Response(status_code=400)

    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            names = {c.get("wa_id"): c.get("profile", {}).get("name", "") for c in value.get("contacts", [])}
            for msg in value.get("messages", []):
                wa_id = msg.get("from")
                if wa_id:
                    _spawn(chatbot.handle_inbound(wa_id, names.get(wa_id, ""), msg))
            for st in value.get("statuses", []):
                chatbot.handle_status(st)
    with db_session() as db:
        cfg.set_value(db, "wa_last_webhook", utcnow().isoformat(timespec="seconds"))
        db.commit()
    # A Meta reenvia a notificação se não receber 200 rapidamente; a resposta da IA roda em segundo plano.
    return Response(status_code=200)
