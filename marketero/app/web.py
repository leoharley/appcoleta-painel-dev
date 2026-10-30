"""Utilitários das páginas: templates, links com o prefixo /marketero, mensagens e datas."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from .config import BASE_DIR, ROOT_PATH, TIMEZONE
from .phones import pretty
from .security import csrf_token

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
TZ = ZoneInfo(TIMEZONE)


def url(path: str = "/") -> str:
    return ROOT_PATH + (path if path.startswith("/") else "/" + path)


def fmt_dt(value: datetime | None, fmt: str = "%d/%m/%Y %H:%M") -> str:
    if not value:
        return ""
    return value.replace(tzinfo=timezone.utc).astimezone(TZ).strftime(fmt)


templates.env.globals["url"] = url
templates.env.filters["dt"] = fmt_dt
templates.env.filters["phone"] = pretty

STATUS_LABELS = {
    "draft": "Rascunho", "running": "Enviando", "paused": "Pausada", "finished": "Concluída",
    "pending": "Na fila", "sending": "Enviando", "queued": "Na fila do Kentro", "sent": "Enviada", "delivered": "Entregue", "read": "Lida", "replied": "Respondeu",
    "failed": "Falhou", "skipped": "Pulada", "received": "Recebida", "simulated": "Simulada",
    "unknown": "Não verificado", "valid": "Tem WhatsApp", "invalid": "Sem WhatsApp",
    "done": "Concluída", "error": "Erro",
}
templates.env.filters["label"] = lambda s: STATUS_LABELS.get(s, s)


def flash(request: Request, message: str, kind: str = "success") -> None:
    # As mensagens viajam no cookie de sessão (limite de ~4 KB no navegador): tamanho e quantidade limitados.
    message = message if len(message) <= 600 else message[:597] + "..."
    request.session["flash"] = (request.session.get("flash", []) + [[kind, message]])[-4:]


def render(request: Request, name: str, **ctx):
    ctx.setdefault("flashes", request.session.pop("flash", []))
    ctx["csrf_token"] = csrf_token(request)
    ctx["logged_in"] = bool(request.session.get("uid"))
    ctx["username"] = request.session.get("username", "")
    return templates.TemplateResponse(request, name, ctx)


def redirect(path: str) -> RedirectResponse:
    return RedirectResponse(url(path), status_code=303)
