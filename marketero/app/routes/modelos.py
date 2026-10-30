"""Templates de mensagem do WhatsApp: cadastro, situação na Meta e exclusão (Cloud API direta)."""
import re
import unicodedata

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from .. import settings_store as cfg
from ..db import get_db
from ..security import check_csrf, require_login
from ..web import flash, redirect, render
from ..whatsapp import WhatsAppError, client_from_settings

router = APIRouter(prefix="/modelos")

CATEGORIES = (("MARKETING", "Marketing (divulgação, convites, novidades)"),
              ("UTILITY", "Utilidade (confirmação ou atualização de algo que a pessoa pediu)"))
STATUS = {"APPROVED": ("Aprovado", "success"), "PENDING": ("Em análise", "warning"),
          "REJECTED": ("Rejeitado", "danger"), "PAUSED": ("Pausado", "secondary"),
          "DISABLED": ("Desativado", "secondary"), "IN_APPEAL": ("Em recurso", "info")}
VAR_RE = re.compile(r"\{\{\s*(\d+)\s*\}\}")
FORM_KEYS = ("name", "category", "language", "header", "body", "examples", "footer", "quick_replies",
             "url_text", "url")


def normalize_name(raw: str) -> str:
    """A Meta só aceita letras minúsculas, números e _ no nome."""
    s = "".join(c for c in unicodedata.normalize("NFD", raw.strip().lower()) if unicodedata.category(c) != "Mn")
    s = re.sub(r"[^a-z0-9_]+", "_", s)
    return re.sub(r"_+", "_", s).strip("_")[:512]


def build_payload(form: dict) -> tuple[dict, list[str]]:
    """Monta o corpo do POST /{waba}/message_templates; devolve (payload, erros)."""
    errors = []
    name = normalize_name(form.get("name", ""))
    if not name:
        errors.append("Informe o nome do template.")
    category = form.get("category") or "MARKETING"
    if category not in dict(CATEGORIES):
        errors.append("Categoria inválida.")
    language = (form.get("language") or "pt_BR").strip()

    body = form.get("body", "").strip()
    if not body:
        errors.append("Escreva o texto da mensagem.")
    elif len(body) > 1024:
        errors.append(f"O texto tem {len(body)} caracteres; o máximo é 1024.")
    nums = [int(n) for n in VAR_RE.findall(body)]
    n_vars = max(nums) if nums else 0
    if nums and sorted(set(nums)) != list(range(1, n_vars + 1)):
        errors.append("As variáveis precisam ser numeradas em sequência: {{1}}, {{2}}, {{3}}…")
    if nums and (VAR_RE.match(body) or re.search(r"\{\{\s*\d+\s*\}\}$", body)):
        errors.append("A Meta não aceita texto que começa ou termina com uma variável; "
                      "coloque palavras antes e depois (ex.: \"Olá {{1}}, tudo bem?\").")
    examples = [e.strip() for e in form.get("examples", "").splitlines() if e.strip()]
    if n_vars and len(examples) < n_vars:
        errors.append(f"O texto tem {n_vars} variável(is): informe {n_vars} exemplo(s), um por linha.")

    header = form.get("header", "").strip()
    if len(header) > 60:
        errors.append("O cabeçalho tem no máximo 60 caracteres.")
    if VAR_RE.search(header):
        errors.append("O cabeçalho não pode ter variáveis.")
    footer = form.get("footer", "").strip()
    if len(footer) > 60:
        errors.append("O rodapé tem no máximo 60 caracteres.")

    buttons = []
    for text in [q.strip() for q in form.get("quick_replies", "").splitlines() if q.strip()]:
        if len(text) > 25:
            errors.append(f"Botão \"{text}\": no máximo 25 caracteres.")
        buttons.append({"type": "QUICK_REPLY", "text": text[:25]})
    url_text, url = form.get("url_text", "").strip(), form.get("url", "").strip()
    if url_text or url:
        if not (url_text and url.startswith("https://")):
            errors.append("Botão de link: informe o texto e um endereço começando com https://")
        elif len(url_text) > 25:
            errors.append("Texto do botão de link: no máximo 25 caracteres.")
        buttons.append({"type": "URL", "text": url_text[:25], "url": url})
    if len([b for b in buttons if b["type"] == "QUICK_REPLY"]) > 3:
        errors.append("No máximo 3 botões de resposta rápida.")

    components = []
    if header:
        components.append({"type": "HEADER", "format": "TEXT", "text": header})
    body_comp: dict = {"type": "BODY", "text": body}
    if n_vars:
        body_comp["example"] = {"body_text": [examples[:n_vars]]}
    components.append(body_comp)
    if footer:
        components.append({"type": "FOOTER", "text": footer})
    if buttons:
        components.append({"type": "BUTTONS", "buttons": buttons})
    payload = {"name": name, "language": language, "category": category, "components": components}
    return payload, errors


def _summary(t: dict) -> dict:
    comps = {c.get("type"): c for c in t.get("components", [])}
    label, color = STATUS.get(t.get("status", ""), (t.get("status", ""), "light"))
    reason = t.get("rejected_reason") or ""
    return {"name": t.get("name", ""), "language": t.get("language", ""), "category": (t.get("category") or "").lower(),
            "status": label, "color": color, "reason": "" if reason in ("", "NONE") else reason,
            "header": comps.get("HEADER", {}).get("text", ""), "body": comps.get("BODY", {}).get("text", ""),
            "footer": comps.get("FOOTER", {}).get("text", ""),
            "buttons": [b.get("text", "") for b in comps.get("BUTTONS", {}).get("buttons", [])]}


async def _page(request: Request, db: Session, values: dict | None = None, errors: list[str] | None = None):
    is_meta = (cfg.get(db, "wa_provider") or "kentro") == "meta"
    items, load_error = [], ""
    if is_meta:
        try:
            items = [_summary(t) for t in await client_from_settings(db).list_templates()]
        except WhatsAppError as e:
            load_error = str(e)
    return render(request, "modelos.html", is_meta=is_meta, items=items, load_error=load_error,
                  categories=CATEGORIES, values=values or {"category": "MARKETING", "language": "pt_BR"},
                  errors=errors or [])


@router.get("")
async def list_templates(request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    return await _page(request, db)


@router.post("/criar")
async def create(request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    form = await check_csrf(request)
    values = {k: str(form.get(k, "")) for k in FORM_KEYS}
    payload, errors = build_payload(values)
    if not errors:
        try:
            res = await client_from_settings(db).create_template(payload)
        except WhatsAppError as e:
            errors = [f"A Meta recusou o template: {e}"]
        else:
            label = STATUS.get(res.get("status", ""), (res.get("status", "enviado"),))[0]
            flash(request, f"Template \"{payload['name']}\" enviado para a Meta. Situação: {label}. "
                           "A aprovação costuma sair em minutos; clique em Atualizar para ver.")
            return redirect("/modelos")
    return await _page(request, db, values, errors)


@router.post("/excluir")
async def delete(request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    form = await check_csrf(request)
    name = str(form.get("name", ""))
    try:
        await client_from_settings(db).delete_template(name)
        flash(request, f"Template \"{name}\" excluído na Meta (em todos os idiomas).")
    except WhatsAppError as e:
        flash(request, f"Não foi possível excluir: {e}", "danger")
    return redirect("/modelos")
