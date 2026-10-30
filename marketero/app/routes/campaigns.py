from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Campaign, CampaignRecipient, Contact, utcnow
from ..security import check_csrf, require_login
from ..web import TZ, flash, redirect, render
from ..whatsapp import render_template_body
from ..dispatcher import eligible, render_params
from ..messaging import MessagingError, get_channel
from .contacts import all_tags

router = APIRouter(prefix="/campanhas")
MAX_RATE = 12  # o disparador verifica a fila a cada 5 s


def audience(db: Session, tags: list[str]) -> list[Contact]:
    stmt = select(Contact).where(Contact.phone.is_not(None), Contact.opted_out.is_(False),
                                 Contact.whatsapp_status != "invalid")
    rows = db.scalars(stmt.order_by(Contact.id)).all()
    if tags:
        wanted = set(tags)
        rows = [c for c in rows if wanted & set(c.tag_list())]
    return rows


def counts(db: Session, campaign_id: int) -> dict:
    rows = db.execute(select(CampaignRecipient.status, func.count()).where(CampaignRecipient.campaign_id == campaign_id)
                      .group_by(CampaignRecipient.status)).all()
    d = {s: n for s, n in rows}
    d["total"] = sum(d.values())
    return d


@router.get("")
async def list_campaigns(request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    camps = db.scalars(select(Campaign).order_by(Campaign.id.desc())).all()
    return render(request, "campaigns.html", campaigns=camps, counts={c.id: counts(db, c.id) for c in camps})


@router.get("/nova")
async def new_campaign(request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    channel = get_channel(db)
    templates, error = [], ""
    try:
        templates = await channel.list_templates()
    except MessagingError as e:
        error = str(e)
    return render(request, "campaign_form.html", templates=templates, templates_error=error, tags=all_tags(db),
                  max_rate=MAX_RATE, channel=channel)


@router.post("/criar")
async def create_campaign(request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    form = await check_csrf(request)
    name = str(form.get("name", "")).strip()
    tmpl = str(form.get("template", "")).strip()  # "id|idioma|nome" vindo da lista
    if tmpl.count("|") >= 2:
        tmpl_ref, lang, tmpl_name = tmpl.split("|", 2)
    else:  # informado à mão (lista indisponível)
        tmpl_ref = tmpl_name = str(form.get("template_name", "")).strip()
        lang = str(form.get("template_language", "pt_BR")).strip()
    if not name or not tmpl_name:
        flash(request, "Informe o nome da campanha e o template.", "danger")
        return redirect("/campanhas/nova")
    tags = [t for t in form.getlist("tags") if t]
    start_raw = str(form.get("start_at", "")).strip()
    start_at = None
    if start_raw:
        start_at = (datetime.fromisoformat(start_raw).replace(tzinfo=TZ)
                    .astimezone(timezone.utc).replace(tzinfo=None))
    try:
        rate = min(MAX_RATE, max(1, int(form.get("rate_per_minute") or 6)))
    except ValueError:
        rate = 6
    camp = Campaign(name=name, template_name=tmpl_name, template_ref=tmpl_ref, template_language=lang or "pt_BR",
                    template_body=str(form.get("template_body", "")).strip(),
                    variables=str(form.get("variables", "")).strip(), ai_context=str(form.get("ai_context", "")).strip(),
                    filter_tags=", ".join(tags), rate_per_minute=rate, start_at=start_at)
    db.add(camp)
    db.flush()
    contacts = audience(db, tags)
    for c in contacts:
        db.add(CampaignRecipient(campaign_id=camp.id, contact_id=c.id))
    db.commit()
    flash(request, f"Campanha criada com {len(contacts)} destinatário(s). Confira a prévia e clique em Iniciar.")
    return redirect(f"/campanhas/{camp.id}")


@router.get("/{cid:int}")
async def view_campaign(cid: int, request: Request, status: str = "", db: Session = Depends(get_db),
                        uid: int = Depends(require_login)):
    camp = db.get(Campaign, cid)
    if not camp:
        return redirect("/campanhas")
    stmt = select(CampaignRecipient).where(CampaignRecipient.campaign_id == cid)
    if status:
        stmt = stmt.where(CampaignRecipient.status == status)
    recs = db.scalars(stmt.order_by(CampaignRecipient.id).limit(500)).all()
    preview = None
    first = next((r for r in recs if not eligible(r.contact)), None)
    if first:
        params = render_params(camp, first.contact)
        preview = (first.contact, render_template_body(camp.template_body, params) if camp.template_body else "",
                   params)
    return render(request, "campaign_view.html", c=camp, recs=recs, counts=counts(db, cid), status=status,
                  preview=preview, simulation=get_channel(db).simulation)


@router.post("/{cid:int}/acao")
async def campaign_action(cid: int, request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    form = await check_csrf(request)
    camp = db.get(Campaign, cid)
    action = form.get("action")
    if not camp:
        return redirect("/campanhas")
    if action == "start" and camp.status in ("draft", "paused"):
        camp.status = "running"
        flash(request, "Campanha em andamento. Os envios respeitam o horário, o ritmo e o limite diário configurados.")
    elif action == "pause" and camp.status == "running":
        camp.status = "paused"
        flash(request, "Campanha pausada.")
    elif action == "retry_failed":
        n = 0
        for r in db.scalars(select(CampaignRecipient).where(CampaignRecipient.campaign_id == cid,
                                                            CampaignRecipient.status == "failed")).all():
            r.status, r.error = "pending", ""
            n += 1
        if n and camp.status == "finished":
            camp.status = "paused"
        flash(request, f"{n} envio(s) com falha voltaram para a fila.")
    elif action == "refresh_audience":
        existing = {r.contact_id for r in camp.recipients}
        tags = [t.strip() for t in camp.filter_tags.split(",") if t.strip()]
        new = [c for c in audience(db, tags) if c.id not in existing]
        for c in new:
            db.add(CampaignRecipient(campaign_id=cid, contact_id=c.id))
        if new and camp.status == "finished":
            camp.status = "paused"
        flash(request, f"{len(new)} contato(s) novo(s) incluído(s).")
    elif action == "delete" and camp.status != "running":
        db.delete(camp)
        db.commit()
        flash(request, "Campanha excluída.")
        return redirect("/campanhas")
    db.commit()
    return redirect(f"/campanhas/{cid}")
