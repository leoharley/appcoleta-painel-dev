"""Login, painel inicial, configurações (inclui base de conhecimento .md) e troca de senha."""
import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import ai
from .. import settings_store as cfg
from ..chatbot import build_static_prompt
from ..config import PUBLIC_URL
from ..db import get_db
from ..models import (Campaign, CampaignRecipient, Contact, Conversation,
                      KnowledgeDoc, Message, User, utcnow)
from .. import kentro_poller
from ..messaging import MessagingError, get_channel
from ..security import (check_csrf, clear_failures, hash_password,
                        login_blocked, register_failure, require_login,
                        verify_password)
from ..web import flash, fmt_dt, redirect, render

router = APIRouter()
KB_EXTENSIONS = (".md", ".markdown")
KB_MAX_BYTES = 512 * 1024  # por arquivo; tudo que está ativo vai em cada resposta da IA


@router.get("/login")
async def login_form(request: Request):
    return render(request, "login.html")


@router.post("/login")
async def login(request: Request, db: Session = Depends(get_db)):
    form = await check_csrf(request)
    # IP real: o uvicorn (--proxy-headers, confiando só no Apache local) já resolve o X-Forwarded-For.
    # Ler o cabeçalho direto permitiria ao atacante escolher o IP e burlar o limite de tentativas.
    ip = request.client.host if request.client else "?"
    if login_blocked(ip):
        flash(request, "Muitas tentativas. Aguarde 15 minutos.", "danger")
        return redirect("/login")
    user = db.scalar(select(User).where(User.username == str(form.get("username", "")).strip()))
    if not user or not verify_password(str(form.get("password", "")), user.password_hash):
        register_failure(ip)
        flash(request, "Usuário ou senha inválidos.", "danger")
        return redirect("/login")
    clear_failures(ip)
    request.session.clear()
    request.session.update(uid=user.id, username=user.username)
    return redirect("/")


@router.post("/logout")
async def logout(request: Request):
    await check_csrf(request)
    request.session.clear()
    return redirect("/login")


@router.get("/")
async def dashboard(request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    since = utcnow() - timedelta(days=1)
    stats = {
        "contacts": db.scalar(select(func.count()).select_from(Contact)),
        "with_phone": db.scalar(select(func.count()).select_from(Contact).where(Contact.phone.is_not(None))),
        "opted_out": db.scalar(select(func.count()).select_from(Contact).where(Contact.opted_out.is_(True))),
        "conversations": db.scalar(select(func.count()).select_from(Conversation)),
        "unread": db.scalar(select(func.coalesce(func.sum(Conversation.unread), 0))),
        "running": db.scalar(select(func.count()).select_from(Campaign).where(Campaign.status == "running")),
        "msgs_in_24h": db.scalar(select(func.count()).select_from(Message)
                                 .where(Message.direction == "in", Message.created_at >= since)),
        "sent_24h": db.scalar(select(func.count()).select_from(CampaignRecipient)
                              .where(CampaignRecipient.sent_at >= since)),
    }
    conf = ai.config_from_settings(db)
    channel = get_channel(db)
    checklist = [
        ("IA configurada", conf.configured, conf.label if conf.configured else "Informe a chave e o modelo em Configurações"),
        (f"WhatsApp via {channel.label}", channel.configured,
         "OK" if channel.configured else ("Informe a fila e a chave da API do Kentro" if channel.key == "kentro"
                                          else "Phone Number ID e token")),
    ]
    if channel.key == "kentro":
        st = kentro_poller.STATUS
        ok_txt = f"última leitura {fmt_dt(st['last_ok'], '%d/%m %H:%M:%S')}, {st['open_chats']} atendimento(s) aberto(s)"
        checklist.append(("Leitura das mensagens do Kentro", bool(st["last_ok"]) and not st["last_error"],
                          st["last_error"] or (ok_txt if st["last_ok"] else "aguardando configuração")))
    else:
        last_hook = cfg.get(db, "wa_last_webhook")
        checklist += [
            ("App Secret do webhook", cfg.is_set(db, "wa_app_secret"), "Necessário para aceitar as notificações da Meta"),
            ("Webhook verificado pela Meta", cfg.get_bool(db, "wa_webhook_verified"), f"{PUBLIC_URL}/webhook/whatsapp"),
            ("Última notificação recebida", bool(last_hook),
             datetime.fromisoformat(last_hook).strftime("%d/%m/%Y %H:%M UTC") if last_hook else "nenhuma ainda"),
        ]
    checklist.append(("Modo simulação", not channel.simulation,
                      "Ligado: nada é enviado de verdade" if channel.simulation else "Desligado (envios reais)"))
    return render(request, "dashboard.html", stats=stats, checklist=checklist)


@router.get("/configuracoes")
async def settings_home(request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    return _settings_page(request, db, cfg.SECTIONS[0][0])


@router.get("/configuracoes/{section}")
async def settings_form(section: str, request: Request, db: Session = Depends(get_db),
                        uid: int = Depends(require_login)):
    if section not in cfg.SECTION_GROUPS:
        return redirect("/configuracoes")
    return _settings_page(request, db, section)


def _settings_page(request: Request, db: Session, section: str):
    values = {f.key: ("" if f.kind == "secret" else cfg.get(db, f.key)) for f in cfg.FIELDS}
    secret_set = {f.key: cfg.is_set(db, f.key) for f in cfg.FIELDS if f.kind == "secret"}
    docs = db.scalars(select(KnowledgeDoc).order_by(KnowledgeDoc.name)).all() if section == "atendimento" else []
    active_chars = sum(len(d.content) for d in docs if d.enabled)
    return render(request, "settings.html", fields=cfg.FIELDS, values=values, secret_set=secret_set,
                  sections=cfg.SECTIONS, section=section, section_groups=cfg.SECTION_GROUPS[section],
                  webhook_url=f"{PUBLIC_URL}/webhook/whatsapp", docs=docs, active_chars=active_chars,
                  kb_max_kb=KB_MAX_BYTES // 1024, max_delay=cfg.MAX_REPLY_DELAY)


@router.post("/configuracoes")
async def settings_save(request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    form = await check_csrf(request)
    sections = _save_settings(db, form)
    flash(request, "Configurações salvas.")
    return redirect(f"/configuracoes/{sections[0]}" if len(sections) == 1 else "/configuracoes")


def _form_sections(form) -> list[str]:
    """Subtelas cujos campos vieram no formulário (o campo oculto "section" ou qualquer campo dela)."""
    keys = set(form.keys())
    found = [s for s, _t, groups in cfg.SECTIONS
             if form.get("section") == s or any(f.key in keys for f in cfg.FIELDS if f.group in groups)]
    return found


def _save_settings(db: Session, form) -> list[str]:
    """Grava só os campos das subtelas enviadas: interruptores de outras subtelas não são desligados."""
    sections = _form_sections(form)
    groups = {g for s in sections for g in cfg.SECTION_GROUPS[s]}
    for f in cfg.FIELDS:
        if f.group not in groups:
            continue
        if f.kind == "bool":
            cfg.set_value(db, f.key, "1" if form.get(f.key) else "0")
        elif f.kind == "secret":
            val = str(form.get(f.key, "")).strip()
            if form.get(f.key + "__clear"):
                cfg.set_value(db, f.key, "")
            elif val:  # vazio = manter o valor atual
                cfg.set_value(db, f.key, val)
        elif f.key in form:
            val = str(form.get(f.key, "")).strip()
            if f.kind == "int":
                try:
                    val = max(0, int(val))
                    if f.key.startswith("reply_delay_"):
                        val = min(val, cfg.MAX_REPLY_DELAY)
                    val = str(val)
                except ValueError:
                    val = f.default
            cfg.set_value(db, f.key, val)
    db.commit()
    return sections


@router.post("/configuracoes/conhecimento")
async def kb_upload(request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    form = await check_csrf(request)
    created = updated = 0
    problems = []
    for up in form.getlist("files"):
        name = os.path.basename(getattr(up, "filename", "") or "").strip()[:200]
        if not name:
            continue
        if not name.lower().endswith(KB_EXTENSIONS):
            problems.append(f"{name}: use arquivos .md")
            continue
        data = await up.read(KB_MAX_BYTES + 1)
        if len(data) > KB_MAX_BYTES:
            problems.append(f"{name}: maior que {KB_MAX_BYTES // 1024} KB")
            continue
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = data.decode("latin-1")
        text = text.replace("\r\n", "\n").strip()
        if not text:
            problems.append(f"{name}: arquivo vazio")
            continue
        doc = db.scalar(select(KnowledgeDoc).where(KnowledgeDoc.name == name))
        if doc:
            doc.content, doc.enabled = text, True
            updated += 1
        else:
            db.add(KnowledgeDoc(name=name, content=text))
            created += 1
    db.commit()
    if created or updated:
        flash(request, f"Base de conhecimento: {created} documento(s) importado(s), {updated} substituído(s).")
    if problems:
        flash(request, "Não importados — " + "; ".join(problems), "warning")
    if not (created or updated or problems):
        flash(request, "Escolha ao menos um arquivo .md.", "danger")
    return redirect("/configuracoes/atendimento#conhecimento")


@router.get("/configuracoes/conhecimento/{doc_id:int}")
async def kb_view(doc_id: int, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    doc = db.get(KnowledgeDoc, doc_id)
    if not doc:
        return redirect("/configuracoes/atendimento#conhecimento")
    return PlainTextResponse(doc.content, media_type="text/plain; charset=utf-8")


@router.post("/configuracoes/conhecimento/{doc_id:int}")
async def kb_action(doc_id: int, request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    form = await check_csrf(request)
    doc = db.get(KnowledgeDoc, doc_id)
    if doc:
        action = form.get("action")
        if action == "toggle":
            doc.enabled = not doc.enabled
            flash(request, f"{doc.name}: {'ativado' if doc.enabled else 'desativado'}.")
        elif action == "delete":
            db.delete(doc)
            flash(request, f"{doc.name} excluído.")
        db.commit()
    return redirect("/configuracoes/atendimento#conhecimento")


@router.post("/configuracoes/testar-ia")
async def test_ai(request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    form = await check_csrf(request)
    if "ai_provider" in form:  # veio do formulário de configurações: salva antes de testar
        _save_settings(db, form)
    conf = ai.config_from_settings(db)
    try:
        reply = await ai.generate_reply(conf, build_static_prompt(db),
                                        [{"role": "user", "content": "Olá! Que serviços vocês oferecem?"}])
        flash(request, f"{conf.label} respondeu: {reply[:600]}")
    except ai.AIError as e:
        flash(request, f"Falha ao testar a IA: {e}", "danger")
    return redirect("/configuracoes/ia")


@router.post("/configuracoes/testar-whatsapp")
async def test_whatsapp(request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    form = await check_csrf(request)
    if "wa_provider" in form:
        _save_settings(db, form)
    channel = get_channel(db)
    try:
        flash(request, await channel.test())
    except MessagingError as e:
        flash(request, f"Falha no WhatsApp via {channel.label}: {e}", "danger")
    return redirect("/configuracoes/whatsapp")


@router.get("/senha")
async def password_form(request: Request, uid: int = Depends(require_login)):
    return render(request, "password.html")


@router.post("/senha")
async def password_save(request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    form = await check_csrf(request)
    user = db.get(User, uid)
    new = str(form.get("new", ""))
    if not verify_password(str(form.get("current", "")), user.password_hash):
        flash(request, "Senha atual incorreta.", "danger")
    elif len(new) < 10 or new != form.get("confirm"):
        flash(request, "A nova senha precisa ter pelo menos 10 caracteres e ser igual à confirmação.", "danger")
    else:
        user.password_hash = hash_password(new)
        db.commit()
        flash(request, "Senha alterada.")
        return redirect("/")
    return redirect("/senha")
