"""Disparo das campanhas em segundo plano, respeitando ritmo, horário e limite diário."""
import asyncio
import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import settings_store as cfg
from .chatbot import get_conversation
from .config import TIMEZONE
from .db import db_session
from .messaging import MessagingError, get_channel
from .models import Campaign, CampaignRecipient, Contact, Message, utcnow
from .whatsapp import render_template_body

log = logging.getLogger("marketero.dispatcher")
TICK_SECONDS = 5
TZ = ZoneInfo(TIMEZONE)
_last_send: dict[int, datetime] = {}


def render_params(campaign: Campaign, contact: Contact) -> list[str]:
    values = {
        "nome": (contact.name or "").split(" ")[0] if contact.name else "",
        "nome_completo": contact.name or "",
        "organizacao": contact.organization or "",
        "cargo": contact.role or "",
        "cidade": contact.city or "",
        "uf": contact.uf or "",
    }
    params = []
    for line in (campaign.variables or "").splitlines():
        line = line.strip()
        if not line:
            continue
        for k, v in values.items():
            line = line.replace("{" + k + "}", v)
        params.append(line.strip() or "-")
    return params


def in_send_window(db: Session, now_local: datetime) -> bool:
    if now_local.weekday() >= 5 and not cfg.get_bool(db, "dispatch_weekends"):
        return False
    start = cfg.get_int(db, "dispatch_hour_start", 8)
    end = cfg.get_int(db, "dispatch_hour_end", 19)
    return start <= now_local.hour < end


def sent_today(db: Session) -> int:
    today_local = datetime.now(TZ).date()
    start_utc = datetime.combine(today_local, time.min, TZ).astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return db.scalar(select(func.count()).select_from(CampaignRecipient)
                     .where(CampaignRecipient.sent_at >= start_utc,
                            CampaignRecipient.status.not_in(("failed", "skipped")))) or 0


def eligible(contact: Contact) -> str | None:
    """Motivo para pular o contato, ou None se pode receber."""
    if contact.opted_out:
        return "descadastrado"
    if not contact.phone:
        return "sem telefone"
    if contact.whatsapp_status == "invalid":
        return "número sem WhatsApp"
    return None


async def send_one(db: Session, campaign: Campaign, rec: CampaignRecipient) -> None:
    contact = rec.contact
    reason = eligible(contact)
    if reason:
        rec.status, rec.error = "skipped", reason
        db.commit()
        return
    channel = get_channel(db)
    params = render_params(campaign, contact)
    body = render_template_body(campaign.template_body, params) or f"[Template {campaign.template_name}]"
    conv = get_conversation(db, contact.phone)
    # Grava antes de chamar a API: nenhuma escrita fica aberta (trava do SQLite) durante a chamada externa,
    # e "sending" impede que um destinatário seja reenviado se o processo cair no meio do envio.
    rec.status = "sending"
    db.commit()
    msg = Message(conversation_id=conv.id, direction="out", author="campaign", kind="template", body=body)
    try:
        ext_id, status = await channel.send_template(contact, campaign, params, f"marketero:{rec.id}")
        rec.status, rec.wa_message_id, rec.sent_at = status, ext_id, utcnow()
        msg.wa_message_id = ext_id
        msg.status = "simulated" if channel.simulation else ("sent" if status == "queued" else status)
        conv.campaign_id = campaign.id
    except MessagingError as e:
        rec.status, rec.error, rec.sent_at = "failed", str(e), utcnow()
        msg.status, msg.error = "failed", str(e)
        if e.no_whatsapp:
            contact.whatsapp_status = "invalid"
        if e.opted_out:
            contact.opted_out = True
    except Exception as e:  # erro inesperado: o envio pode ou não ter acontecido; não reenvia sozinho
        log.exception("Falha inesperada no envio do destinatário %s", rec.id)
        rec.status, rec.sent_at = "failed", utcnow()
        rec.error = f"erro inesperado ({e}); confira no WhatsApp antes de reenviar"
        msg.status, msg.error = "failed", rec.error
    db.add(msg)
    conv.last_message_at = utcnow()
    db.commit()


def recover_interrupted(db: Session) -> None:
    """Destinatários presos em "sending" (processo caiu durante o envio) viram falha com aviso."""
    for rec in db.scalars(select(CampaignRecipient).where(CampaignRecipient.status == "sending")).all():
        rec.status, rec.sent_at = "failed", rec.sent_at or utcnow()
        rec.error = "envio interrompido (o sistema reiniciou); pode ter sido enviado — confira antes de reenviar"
    db.commit()


async def tick() -> None:
    with db_session() as db:
        now_utc = utcnow()
        for camp in db.scalars(select(Campaign).where(Campaign.status == "running")).all():
            if camp.start_at and camp.start_at > now_utc:
                continue
            pending = db.scalars(select(CampaignRecipient).where(CampaignRecipient.campaign_id == camp.id,
                                                                 CampaignRecipient.status == "pending")
                                 .order_by(CampaignRecipient.id).limit(1)).first()
            if pending is None:
                camp.status, camp.finished_at = "finished", now_utc
                db.commit()
                log.info("Campanha %s concluída", camp.id)
                continue
            if not in_send_window(db, datetime.now(TZ)):
                continue
            if sent_today(db) >= cfg.get_int(db, "dispatch_daily_limit", 250):
                continue
            # Ritmo: no máximo rate_per_minute envios por minuto, espaçados igualmente.
            interval = timedelta(seconds=60 / max(1, camp.rate_per_minute))
            last = _last_send.get(camp.id)
            if last and now_utc - last < interval:
                continue
            if len(_last_send) > 500:  # campanhas antigas não precisam mais do registro de ritmo
                _last_send.clear()
            _last_send[camp.id] = now_utc
            await send_one(db, camp, pending)


async def run(stop: asyncio.Event) -> None:
    log.info("Disparador iniciado")
    with db_session() as db:
        recover_interrupted(db)
    while not stop.is_set():
        try:
            await tick()
        except Exception:  # o laço não pode morrer por um erro pontual
            log.exception("Erro no disparador")
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
