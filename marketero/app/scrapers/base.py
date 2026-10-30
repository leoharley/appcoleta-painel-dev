"""Extração de contatos de páginas HTML e gravação na base de contatos."""
import asyncio
import ipaddress
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Contact
from ..phones import find_phones, is_mobile

USER_AGENT = ("MarketeroBot/1.0 (+https://institutoalveslima.com.br/marketero; "
              "comercial@institutoalveslima.com.br)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
URL_RE = re.compile(r"(?:https?://|www\.)[^\s<>\"')]+", re.I)
SIGLA_RE = re.compile(r"\(([A-Z][A-Z0-9 ]{1,14})\)|^([A-Z][A-Z0-9]{1,14})\s*[-–—]\s+")
LABEL_RE = {
    "address": re.compile(r"Endere[cç]o\s*:?\s*(.+)", re.I),
    "president": re.compile(r"Presidente(?:\s+nacional)?\s*:?\s*(.+)", re.I),
}
GENERIC_TITLE_RE = re.compile(
    r"^(contatos?|fale conosco|endere[cç]os?|diret[oó]rio( nacional)?|informa[cç][oõ]es( de contato)?|dados|sede)$", re.I)
SKIP_DOMAINS = ("tse.jus.br", "tre-", "gov.br/", "facebook.com/sharer", "twitter.com/intent", "whatsapp.com/send")


class ScrapeBlocked(Exception):
    pass


@dataclass
class ContactRecord:
    name: str = ""
    organization: str = ""
    role: str = ""
    phones: list[str] = field(default_factory=list)
    email: str = ""
    website: str = ""
    address: str = ""
    city: str = ""
    uf: str = ""
    source_ref: str = ""


MAX_PAGE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5
TOTAL_TIMEOUT = 45


async def ensure_public_url(url: str) -> None:
    """Só permite páginas públicas da internet: nada de endereços internos do servidor ou da rede privada
    (127.0.0.1, 10.x, 192.168.x, 169.254.x...) nem portas diferentes de 80/443."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("Endereço inválido: use uma página começando com http:// ou https://.")
    if p.port not in (None, 80, 443):
        raise ValueError("Só são permitidas páginas nas portas padrão (80/443).")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80),
                                                             type=socket.SOCK_STREAM)
    except socket.gaierror as e:
        raise ValueError(f"Não foi possível encontrar o site {p.hostname}.") from e
    for info in infos:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise ValueError("Endereço interno ou privado não é permitido.")


async def fetch(url: str) -> str:
    """Busca uma página pública se identificando honestamente. Bloqueio do site vira ScrapeBlocked."""
    async def _fetch() -> str:
        current = url
        async with httpx.AsyncClient(timeout=20, follow_redirects=False,
                                     headers={"User-Agent": USER_AGENT, "Accept-Language": "pt-BR"}) as c:
            for _ in range(MAX_REDIRECTS + 1):
                await ensure_public_url(current)  # verificado de novo a cada redirecionamento
                async with c.stream("GET", current) as r:
                    if r.is_redirect and r.headers.get("location"):
                        current = urljoin(current, r.headers["location"])
                        continue
                    if r.status_code in (401, 403, 429):
                        raise ScrapeBlocked(
                            f"O site recusou o acesso automático (HTTP {r.status_code}). Abra a página no navegador, "
                            "salve com Ctrl+S (\"Página da Web, somente HTML\") e use a opção de enviar páginas salvas.")
                    r.raise_for_status()
                    chunks, size = [], 0
                    async for chunk in r.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_PAGE_BYTES:
                            raise ValueError("Página maior que 5 MB.")
                        chunks.append(chunk)
                    return b"".join(chunks).decode(r.encoding or "utf-8", errors="replace")
        raise ValueError("Redirecionamentos demais.")

    try:
        return await asyncio.wait_for(_fetch(), TOTAL_TIMEOUT)
    except asyncio.TimeoutError as e:
        raise ValueError(f"A página demorou mais de {TOTAL_TIMEOUT} s para carregar.") from e


def _clean_soup(html: str) -> BeautifulSoup:
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "nav", "footer", "header", "form", "iframe"]):
        t.decompose()
    return soup


def _text(el) -> str:
    return re.sub(r"[ \t\r\f\v]+", " ", el.get_text("\n")).strip()


def _record_from_block(title: str, text: str, source_ref: str) -> ContactRecord | None:
    phones = find_phones(text)
    emails = [e.rstrip(".") for e in EMAIL_RE.findall(text)]
    if not phones and not emails:
        return None
    rec = ContactRecord(source_ref=source_ref)
    title = re.sub(r"\s+", " ", title).strip(" :-–")
    m = SIGLA_RE.search(title)
    sigla = (m.group(1) or m.group(2)).strip() if m else ""
    rec.organization = title if not sigla or sigla in title else f"{title} ({sigla})"
    rec.phones = phones
    rec.email = emails[0] if emails else ""
    sites = [u for u in URL_RE.findall(text) if not any(d in u.lower() for d in SKIP_DOMAINS)]
    if sites:
        rec.website = sites[0].rstrip(".,;")
    for line in text.splitlines():
        for key, rx in LABEL_RE.items():
            mm = rx.match(line.strip())
            if mm and not getattr(rec, "name" if key == "president" else key):
                val = mm.group(1).strip()[:300]
                if key == "president":
                    rec.name, rec.role = val, "Presidente"
                else:
                    rec.address = val
        uf = re.search(r"\b([A-Z][a-zÀ-ú]+(?: [A-Za-zÀ-ú]+)*)\s*[/-]\s*([A-Z]{2})\b", line)
        if uf and not rec.city and rec.address and line.strip() in rec.address:
            rec.city, rec.uf = uf.group(1), uf.group(2)
    return rec


def extract_records(html: str, source_ref: str = "") -> list[ContactRecord]:
    """Divide a página em blocos por título (h1–h4) e extrai os que têm telefone ou e-mail."""
    soup = _clean_soup(html)
    root = soup.find("main") or soup.find(id=re.compile("content|conteudo", re.I)) or soup.body or soup
    found: list[tuple[int, int, ContactRecord]] = []  # (ordem no documento, tamanho do bloco, registro)
    headings = root.find_all(["h1", "h2", "h3", "h4"])
    parents: dict[int, str] = {}
    for order, h in enumerate(headings):
        level = int(h.name[1])
        title = _text(h)
        parents = {lv: t for lv, t in parents.items() if lv < level}
        parent_title = parents[max(parents)] if parents else ""
        parents[level] = title
        if GENERIC_TITLE_RE.match(title) and parent_title:
            title = parent_title
        parts = []
        for sib in h.next_siblings:
            if isinstance(sib, Tag) and sib.name in ("h1", "h2", "h3", "h4") and int(sib.name[1]) <= level:
                break
            if isinstance(sib, Tag) and sib.find(["h1", "h2", "h3", "h4"]):
                inner = sib.find(["h1", "h2", "h3", "h4"])
                if int(inner.name[1]) <= level:
                    break
            parts.append(_text(sib) if isinstance(sib, Tag) else str(sib))
        block = "\n".join(parts)
        rec = _record_from_block(title, block, source_ref)
        if rec:
            found.append((order, len(block), rec))
    # Tabelas (uma linha = um contato)
    for i, tr in enumerate(root.find_all("tr")):
        cells = [_text(td) for td in tr.find_all(["td", "th"])]
        if len(cells) >= 2:
            block = "\n".join(cells[1:])
            rec = _record_from_block(cells[0], block, source_ref)
            if rec:
                found.append((len(headings) + i, len(block), rec))
    if not found:
        title = soup.find("h1") or soup.title
        rec = _record_from_block(_text(title) if title else source_ref, _text(root), source_ref)
        if rec:
            found.append((0, 0, rec))
    # Mesmo telefone principal ou e-mail em vários blocos (ex.: título da página que engloba os partidos):
    # fica o bloco mais específico, isto é, o menor.
    seen, unique = set(), []
    for order, _size, r in sorted(found, key=lambda x: x[1]):
        keys = {r.phones[0] if r.phones else "", r.email.lower()} - {""}
        if keys and not (keys & seen):
            seen |= keys
            unique.append((order, r))
    return [r for _, r in sorted(unique, key=lambda x: x[0])]


def save_records(db: Session, records: list[ContactRecord], source: str, tags: str) -> tuple[int, int]:
    """Cria ou completa contatos. Devolve (criados, atualizados)."""
    created = updated = 0
    tag_set = {t.strip() for t in tags.split(",") if t.strip()}
    for r in records:
        phone = r.phones[0] if r.phones else None
        contact = None
        if phone:
            contact = db.scalar(select(Contact).where(Contact.phone == phone))
        if contact is None and r.email:
            contact = db.scalar(select(Contact).where(Contact.email == r.email))
        is_new = contact is None
        if is_new:
            contact = Contact(phone=phone, phone_is_mobile=is_mobile(phone) if phone else False, source=source)
            db.add(contact)
        for attr in ("name", "organization", "role", "email", "website", "address", "city", "uf"):
            val = getattr(r, attr)
            if val and not getattr(contact, attr):
                setattr(contact, attr, val[:400])
        if phone and not contact.phone:
            contact.phone, contact.phone_is_mobile = phone, is_mobile(phone)
        extra = [p for p in r.phones[1:]]
        if extra:
            note = "Outros telefones: " + ", ".join("+" + p for p in extra)
            if note not in (contact.notes or ""):
                contact.notes = ((contact.notes or "") + "\n" + note).strip()
        contact.source_ref = contact.source_ref or r.source_ref[:400]
        contact.tags = ", ".join(sorted(set(contact.tag_list()) | tag_set))
        db.flush()
        created += is_new
        updated += not is_new
    db.commit()
    return created, updated
