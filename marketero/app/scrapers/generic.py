"""Fonte genérica: extrai contatos (telefones, e-mail, site) de uma página pública qualquer."""
from .base import ContactRecord, extract_records, fetch


class GenericPageSource:
    key = "pagina"
    label = "Página da internet (endereço informado)"
    default_tags = "importado"
    supports_live = True

    async def run_live(self, log, url: str = "") -> list[ContactRecord]:
        if not url.startswith(("http://", "https://")):
            raise ValueError("Informe o endereço completo da página (começando com https://).")
        log(f"Buscando {url}")
        records = extract_records(await fetch(url), url)
        log(f"{len(records)} contato(s) encontrados")
        return records

    def parse_files(self, files: list[tuple[str, str]], log) -> list[ContactRecord]:
        records = []
        for name, html in files:
            found = extract_records(html, f"arquivo:{name}")
            log(f"{name}: {len(found)} contato(s)")
            records.extend(found)
        return records
