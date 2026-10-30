"""Marketero: chatbot de WhatsApp com IA, coleta de contatos e campanhas de disparo.

Execução: uvicorn app.main:app (ver deploy/marketero.service)
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import dispatcher, kentro_poller
from . import settings_store as cfg
from .config import BASE_DIR, COOKIE_SECURE, ROOT_PATH, SECRET_KEY
from .db import db_session, init_db
from .routes import admin, campaigns, contacts, imports, inbox, modelos, webhook
from .security import CsrfError, LoginRequired
from .web import flash, redirect

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    with db_session() as db:
        cfg.ensure_defaults(db)
    stop = asyncio.Event()
    tasks = [asyncio.create_task(dispatcher.run(stop)), asyncio.create_task(kentro_poller.run(stop))]
    yield
    stop.set()
    await asyncio.gather(*tasks)


app = FastAPI(title="Marketero", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY, session_cookie="marketero_session",
                   max_age=12 * 3600, same_site="lax", https_only=COOKIE_SECURE, path=ROOT_PATH or "/")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

for r in (webhook.router, admin.router, contacts.router, imports.router, campaigns.router, modelos.router,
          inbox.router):
    app.include_router(r)


@app.exception_handler(LoginRequired)
async def _login_required(request: Request, _exc):
    return redirect("/login")


@app.exception_handler(CsrfError)
async def _csrf(request: Request, _exc):
    flash(request, "Formulário expirado. Tente de novo.", "warning")
    return redirect("/")


@app.get("/saude", response_class=HTMLResponse)
async def health():
    return "ok"
