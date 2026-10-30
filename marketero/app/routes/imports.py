"""Importação de contatos por coleta em sites (TSE e páginas genéricas)."""
import asyncio
import logging

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import db_session, get_db
from ..models import ImportJob, utcnow
from ..scrapers import SOURCES, save_records
from ..scrapers.base import ScrapeBlocked
from ..security import check_csrf, require_login
from ..web import flash, redirect, render

log = logging.getLogger("marketero.imports")
router = APIRouter(prefix="/importar")
_tasks: set[asyncio.Task] = set()
MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_FILES = 50


class JobLog:
    def __init__(self, job_id: int):
        self.job_id = job_id

    def __call__(self, line: str) -> None:
        with db_session() as db:
            job = db.get(ImportJob, self.job_id)
            job.log = (job.log + "\n" + line).strip()
            db.commit()


def _finish(job_id: int, status: str, records=None, tags: str = "", source: str = "", error: str = "") -> None:
    with db_session() as db:
        job = db.get(ImportJob, job_id)
        if records is not None:
            created, updated = save_records(db, records, source, tags)
            job.found, job.created, job.updated = len(records), created, updated
            job.log += f"\nResultado: {len(records)} encontrados, {created} novos, {updated} já existiam (completados)."
        if error:
            job.log += f"\nERRO: {error}"
        job.status, job.finished_at = status, utcnow()
        db.commit()


async def _run_live(job_id: int, source_key: str, url: str, tags: str) -> None:
    src = SOURCES[source_key]
    logger = JobLog(job_id)
    try:
        records = await (src.run_live(logger, url=url) if source_key == "pagina" else src.run_live(logger))
        _finish(job_id, "done", records, tags, source_key)
    except ScrapeBlocked as e:
        _finish(job_id, "error", error=str(e))
    except Exception as e:
        log.exception("Importação %s falhou", job_id)
        _finish(job_id, "error", error=str(e))


@router.get("")
async def import_page(request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    jobs = db.scalars(select(ImportJob).order_by(ImportJob.id.desc()).limit(20)).all()
    return render(request, "import.html", sources=SOURCES.values(), jobs=jobs)


@router.post("/coletar")
async def start_live(request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    form = await check_csrf(request)
    key = str(form.get("source", ""))
    if key not in SOURCES:
        return redirect("/importar")
    url = str(form.get("url", "")).strip()
    tags = str(form.get("tags", "")).strip() or SOURCES[key].default_tags
    job = ImportJob(source=key, description=url or SOURCES[key].label, log="Iniciado")
    db.add(job)
    db.commit()
    task = asyncio.create_task(_run_live(job.id, key, url, tags))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    flash(request, "Coleta iniciada. Acompanhe o andamento abaixo (atualize a página).")
    return redirect("/importar")


@router.post("/arquivos")
async def upload_pages(request: Request, db: Session = Depends(get_db), uid: int = Depends(require_login)):
    form = await check_csrf(request)
    key = str(form.get("source", ""))
    if key not in SOURCES:
        return redirect("/importar")
    tags = str(form.get("tags", "")).strip() or SOURCES[key].default_tags
    files = []
    uploads = [up for up in form.getlist("files") if getattr(up, "filename", "")]
    if len(uploads) > MAX_FILES:
        flash(request, f"Envie no máximo {MAX_FILES} arquivos por vez.", "danger")
        return redirect("/importar")
    for up in uploads:
        data = await up.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            flash(request, f"{up.filename}: arquivo maior que 5 MB, ignorado.", "warning")
            continue
        files.append((up.filename, data.decode("utf-8", errors="replace")))
    if not files:
        flash(request, "Escolha ao menos um arquivo .html salvo pelo navegador.", "danger")
        return redirect("/importar")
    job = ImportJob(source=key, description=f"{len(files)} arquivo(s) enviados", log="Lendo arquivos")
    db.add(job)
    db.commit()
    try:
        records = SOURCES[key].parse_files(files, JobLog(job.id))
        _finish(job.id, "done", records, tags, key)
        flash(request, "Arquivos processados.")
    except Exception as e:
        _finish(job.id, "error", error=str(e))
        flash(request, f"Falha ao ler os arquivos: {e}", "danger")
    return redirect("/importar")
