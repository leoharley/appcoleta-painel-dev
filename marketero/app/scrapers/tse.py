"""Fonte: partidos políticos registrados no TSE (contatos institucionais dos diretórios nacionais).

Coleta apenas dados institucionais publicados pelo próprio TSE (telefone, e-mail, site e
endereço do diretório nacional). Não coleta dados pessoais de dirigentes (CPF etc.).

O portal da Justiça Eleitoral costuma bloquear acessos automáticos vindos de servidores;
nesse caso o sistema avisa e o usuário envia as páginas salvas pelo navegador.
"""
import asyncio
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from .base import ContactRecord, extract_records, fetch

LIST_URL = "https://www.tse.jus.br/partidos/partidos-registrados-no-tse/registrados-no-tse"
DETAIL_PREFIX = "/partidos/partidos-registrados-no-tse/"
DELAY_SECONDS = 2  # pausa entre páginas, para não sobrecarregar o site


def party_links(html: str, base_url: str = LIST_URL) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    links = []
    for a in soup.find_all("a", href=True):
        url = urljoin(base_url, a["href"]).split("#")[0].split("?")[0]
        path = urlparse(url).path.rstrip("/")
        if (DETAIL_PREFIX in path + "/" and not path.endswith("registrados-no-tse")
                and "tse.jus.br" in url and url not in links):
            links.append(url)
    return links


class TSEPartidosSource:
    key = "tse_partidos"
    label = "TSE — Partidos políticos registrados (diretórios nacionais)"
    default_tags = "partido, tse"
    supports_live = True

    async def run_live(self, log) -> list[ContactRecord]:
        log(f"Buscando a lista de partidos em {LIST_URL}")
        html = await fetch(LIST_URL)
        records = extract_records(html, LIST_URL)
        links = party_links(html)
        log(f"{len(links)} páginas de partidos encontradas na lista")
        for i, url in enumerate(links, 1):
            await asyncio.sleep(DELAY_SECONDS)
            try:
                found = extract_records(await fetch(url), url)
            except Exception as e:  # uma página com erro não interrompe as demais
                log(f"[{i}/{len(links)}] erro em {url}: {e}")
                continue
            log(f"[{i}/{len(links)}] {url}: {len(found)} contato(s)")
            records.extend(found)
        return records

    def parse_files(self, files: list[tuple[str, str]], log) -> list[ContactRecord]:
        records = []
        for name, html in files:
            found = extract_records(html, f"arquivo:{name}")
            log(f"{name}: {len(found)} contato(s)")
            if not found and party_links(html):
                log(f"{name}: parece ser a lista de partidos (só links). Salve também a página de cada partido.")
            records.extend(found)
        return records
