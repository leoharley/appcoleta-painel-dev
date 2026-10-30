import csv
import io
import unicodedata

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Contact, Conversation, utcnow
from ..phones import is_mobile, normalize
from ..security import check_csrf, require_login
from ..web import flash, redirect, render

router = APIRouter(prefix="/contatos")
PAGE_SIZE = 50
MAX_CSV_BYTES = 10 * 1024 * 1024
FIELDS = ("name", "organization", "role", "email", "website", "address", "city", "uf", "tags", "notes")

# Cabeçalhos aceitos na importação de CSV (sem acento, minúsculas)
CSV_ALIASES = {
    "name": ("nome", "name", "contato", "responsavel", "presidente"),
    "organization": ("organizacao", "empresa", "partido", "orgao", "instituicao", "organization"),
    "role": ("cargo", "funcao", "role"),
    "phone": ("whatsapp", "celular", "telefone", "fone", "phone", "numero"),
    "email": ("email", "e-mail", "e_mail"),
    "website": ("site", "website", "url"),
    "address": ("endereco", "address"),
    "city": ("cidade", "municipio", "city"),
    "uf": ("uf", "estado", "state"),
    "tags": ("tags", "etiquetas", "lista", "grupo"),
}


def _key(s: str) -> str:
    s = "".join(c for c in unicodedata.normalize("NFD", s or "") if unicodedata.category(c) != "Mn")
    return s.strip().lower().replace(" ", "_")


def tag_condition(tag: str):
    """Etiqueta exata (as etiquetas ficam gravadas como "a, b, c"); mesma regra do público das campanhas."""
    esc = tag.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return or_(Contact.tags == tag,
               Contact.tags.like(f"{esc}, %", escape="\\"),
               Contact.tags.like(f"%, {esc}", escape="\\"),
               Contact.tags.like(f"%, {esc}, %", escape="\\"))


def _csv_safe(value) -> str:
    """Evita que o Excel interprete o texto como fórmula (=, +, -, @)."""
    s = "" if value is None else str(value)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


def contact_query(q: str, tag: str, status: str):
    stmt = select(Contact)
    if q:
        like = f"%{q}%"
        digits = "".join(ch for ch in q if ch.isdigit())
        conds = [Contact.name.ilike(like), Contact.organization.ilike(like), Contact.email.ilike(like),
                 Contact.city.ilike(like)]
        if digits:
            conds.append(Contact.phone.like(f"%{digits}%"))
        stmt = stmt.where(or_(*conds))
    if tag:
        stmt = stmt.where(tag_condition(tag))
    if status == "optout":
        stmt = stmt.where(Contact.opted_out.is_(True))
    elif status == "sem_telefone":
        stmt = stmt.where(Contact.phone.is_(None))
    elif status in ("valid", "invalid", "unknown"):
        stmt = stmt.where(Contact.whatsapp_status == status)
    return stmt


def all_tags(db: Session) -> list[str]:
    tags = set()
    for (t,) in db.execute(select(Contact.tags).distinct()):
        tags.update(x.strip() for x in (t or "").split(",") if x.strip())
    return sorted(tags)


@router.get("")
async def list_contacts(request: Request, q: str = "", tag: str = "", status: str = "", page: int = 1,
                        db: Session = Depends(get_db), uid: int = Depends(require_login)):
    stmt = contact_query(q, tag, status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    page = max(1, page)
    rows = db.scalars(stmt.order_by(Contact.id.desc()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)).all()
    return render(request, "contacts.html", contacts=rows, total=total, page=page,
                  pages=max(1, -(-total // PAGE_SIZE)), q=q, tag=tag, status=status, tags=all_tags(db))


@router.get("/novo")
async def new_contact(request: Request, uid: int = Depends(require_login)):
    return render(request, "contact_form.html", contact=Contact(tags=""), is_new=True)


@router.get("/{cid:int}")
async def edit_contact(cid: int, request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    contact = db.get(Contact, cid)
    if not contact:
        return redirect("/contatos")
    conv = db.scalar(select(Conversation).where(Conversation.contact_id == cid))
    return render(request, "contact_form.html", contact=contact, is_new=False, conversation=conv)


@router.post("/salvar")
async def save_contact(request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    form = await check_csrf(request)
    cid = str(form.get("id") or "")
    if cid and not cid.isdigit():
        return redirect("/contatos")
    contact = db.get(Contact, int(cid)) if cid else Contact(source="manual")
    if contact is None:
        flash(request, "Contato não encontrado (pode ter sido excluído).", "warning")
        return redirect("/contatos")
    raw_phone = str(form.get("phone", "")).strip()
    phone = normalize(raw_phone) if raw_phone else None
    if raw_phone and not phone:
        flash(request, "Telefone inválido. Use DDD + número, ex.: (38) 99999-8888.", "danger")
        return redirect(f"/contatos/{cid}" if cid else "/contatos/novo")
    other = db.scalar(select(Contact).where(Contact.phone == phone, Contact.id != (contact.id or 0))) if phone else None
    if other:
        flash(request, f"Este telefone já pertence ao contato #{other.id}.", "danger")
        return redirect(f"/contatos/{other.id}")
    for f in FIELDS:
        setattr(contact, f, str(form.get(f, "")).strip())
    contact.uf = contact.uf.upper()[:2]
    contact.phone, contact.phone_is_mobile = phone, (is_mobile(phone) if phone else False)
    optout = bool(form.get("opted_out"))
    if optout and not contact.opted_out:
        contact.opted_out_at = utcnow()
    contact.opted_out = optout
    if contact.id is None:
        db.add(contact)
    db.commit()
    flash(request, "Contato salvo.")
    return redirect(f"/contatos/{contact.id}")


@router.post("/lote")
async def bulk(request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    form = await check_csrf(request)
    ids = [int(i) for i in form.getlist("ids") if str(i).isdigit()]
    action, tag = form.get("action"), str(form.get("tag", "")).strip()
    contacts = db.scalars(select(Contact).where(Contact.id.in_(ids))).all() if ids else []
    for c in contacts:
        if action == "add_tag" and tag:
            c.tags = ", ".join(sorted(set(c.tag_list()) | {tag}))
        elif action == "remove_tag" and tag:
            c.tags = ", ".join(t for t in c.tag_list() if t != tag)
        elif action == "delete":
            db.delete(c)
    db.commit()
    flash(request, f"{len(contacts)} contato(s) atualizados.")
    return redirect("/contatos")


@router.post("/importar-csv")
async def import_csv(request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    form = await check_csrf(request)
    upload = form.get("file")
    extra_tags = {t.strip() for t in str(form.get("tags", "")).split(",") if t.strip()}
    if not upload or not getattr(upload, "filename", ""):
        flash(request, "Escolha um arquivo CSV.", "danger")
        return redirect("/importar")
    data = await upload.read(MAX_CSV_BYTES + 1)
    if len(data) > MAX_CSV_BYTES:
        flash(request, "Arquivo maior que 10 MB. Divida a planilha em partes.", "danger")
        return redirect("/importar")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=";,\t")
    except csv.Error:  # ex.: planilha com uma coluna só
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    header_map = {}
    for h in reader.fieldnames or []:
        for field, aliases in CSV_ALIASES.items():
            if _key(h) in aliases and field not in header_map.values():
                header_map[h] = field
    if "phone" not in header_map.values() and "email" not in header_map.values():
        flash(request, "O CSV precisa de uma coluna de telefone/WhatsApp ou de e-mail.", "danger")
        return redirect("/importar")
    created = updated = invalid = 0
    for row in reader:
        rec = {field: (row.get(h) or "").strip() for h, field in header_map.items()}
        phone = normalize(rec.pop("phone", ""))
        if not phone and not rec.get("email"):
            invalid += 1
            continue
        contact = db.scalar(select(Contact).where(Contact.phone == phone)) if phone else None
        if contact is None and rec.get("email"):
            contact = db.scalar(select(Contact).where(Contact.email == rec["email"]))
        is_new = contact is None
        if is_new:
            contact = Contact(phone=phone, phone_is_mobile=is_mobile(phone) if phone else False, source="csv",
                              source_ref=upload.filename[:400])
            db.add(contact)
        tags = set(contact.tag_list()) | extra_tags | {t.strip() for t in rec.pop("tags", "").split(",") if t.strip()}
        for field, val in rec.items():
            if val and not getattr(contact, field):
                setattr(contact, field, val[:400])
        contact.uf = (contact.uf or "").upper()[:2]
        contact.tags = ", ".join(sorted(tags))
        db.flush()
        created += is_new
        updated += not is_new
    db.commit()
    flash(request, f"CSV importado: {created} novos, {updated} atualizados, {invalid} linhas sem telefone/e-mail válido.")
    return redirect("/contatos")


@router.get("/exportar.csv")
async def export_csv(q: str = "", tag: str = "", status: str = "", db: Session = Depends(get_db),
                     uid: int = Depends(require_login)):
    rows = db.scalars(contact_query(q, tag, status).order_by(Contact.id)).all()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "nome", "organizacao", "cargo", "whatsapp", "email", "site", "endereco", "cidade", "uf",
                "tags", "status_whatsapp", "descadastrado", "origem"])
    for c in rows:
        w.writerow([_csv_safe(v) for v in (c.id, c.name, c.organization, c.role, c.phone or "", c.email, c.website,
                                           c.address, c.city, c.uf, c.tags, c.whatsapp_status,
                                           "sim" if c.opted_out else "não", c.source)])
    return StreamingResponse(iter(["﻿" + buf.getvalue()]), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": "attachment; filename=contatos.csv"})
