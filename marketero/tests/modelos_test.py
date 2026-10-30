"""Cadastro de templates pela tela Templates (a API da Meta é simulada; nenhuma chamada real).

  MARKETERO_DATA_DIR=$(mktemp -d) MARKETERO_COOKIE_SECURE=0 MARKETERO_SECRET_KEY=t python -m tests.modelos_test
"""
import contextlib
import io
import re
import sys

from fastapi.testclient import TestClient

from app import settings_store as cfg
from app.db import db_session, init_db
from app.main import app
from app.routes.modelos import build_payload, normalize_name
from app.whatsapp import WhatsAppClient, WhatsAppError

failures = []
CALLS = []
STORE = [{"id": "1", "name": "hello_world", "status": "APPROVED", "language": "en_US", "category": "UTILITY",
          "components": [{"type": "BODY", "text": "Hello World"}]}]


def check(cond, label):
    print(("OK   " if cond else "FALHA"), label)
    if not cond:
        failures.append(label)


async def fake_request(self, method, path, **kw):
    CALLS.append((method, path, kw))
    if method == "GET":
        return {"data": STORE}
    if method == "POST":
        p = kw["json"]
        if any(t["name"] == p["name"] for t in STORE):
            raise WhatsAppError("Já existe conteúdo neste idioma (código 100)", 100)
        STORE.append({"id": "2", "name": p["name"], "status": "PENDING", "language": p["language"],
                      "category": p["category"], "components": p["components"]})
        return {"id": "2", "status": "PENDING", "category": p["category"]}
    if method == "DELETE":
        STORE[:] = [t for t in STORE if t["name"] != kw["params"]["name"]]
        return {"success": True}


def unit_checks():
    check(normalize_name("Convite Pesquisa Eleitoral!") == "convite_pesquisa_eleitoral", "nome normalizado")
    check(normalize_name("Ação  Março") == "acao_marco", "nome sem acento")
    base = {"name": "x", "category": "MARKETING", "language": "pt_BR"}
    p, e = build_payload({**base, "body": "Olá {{1}}, aqui é do Instituto. Podemos conversar sobre {{2}}?",
                          "examples": "Maria\npesquisas", "header": "Instituto Alves Lima",
                          "footer": "Responda SAIR para não receber", "quick_replies": "Quero saber mais\nSAIR",
                          "url_text": "Site", "url": "https://institutoalveslima.com.br"})
    comps = {c["type"]: c for c in p["components"]}
    check(not e, f"template completo válido {e}")
    check(comps["BODY"]["example"] == {"body_text": [["Maria", "pesquisas"]]}, "exemplos das variáveis no corpo")
    check([b["type"] for b in comps["BUTTONS"]["buttons"]] == ["QUICK_REPLY", "QUICK_REPLY", "URL"], "botões")
    check(comps["HEADER"] == {"type": "HEADER", "format": "TEXT", "text": "Instituto Alves Lima"}, "cabeçalho")
    _, e = build_payload({**base, "body": "{{1}}, tudo bem?", "examples": "Maria"})
    check(any("começa ou termina" in x for x in e), "recusa variável no início")
    _, e = build_payload({**base, "body": "Olá, tudo bem {{1}}", "examples": "Maria"})
    check(any("começa ou termina" in x for x in e), "recusa variável no fim")
    _, e = build_payload({**base, "body": "Olá {{1}} e {{3}}, ok?", "examples": "a\nb\nc"})
    check(any("sequência" in x for x in e), "recusa variáveis fora de sequência")
    _, e = build_payload({**base, "body": "Olá {{1}}, ok?", "examples": ""})
    check(any("exemplo" in x for x in e), "exige exemplo para cada variável")
    _, e = build_payload({**base, "body": "Oi", "quick_replies": "a\nb\nc\nd"})
    check(any("3 botões" in x for x in e), "no máximo 3 respostas rápidas")
    _, e = build_payload({**base, "body": "Oi", "url_text": "Site", "url": "http://x.com"})
    check(any("https://" in x for x in e), "link precisa ser https")
    p, e = build_payload({**base, "body": "Mensagem sem variáveis."})
    check(not e and "example" not in p["components"][0], "texto sem variáveis não leva exemplo")


def web_checks():
    WhatsAppClient._request = fake_request
    with TestClient(app) as c:
        from manage import criar_usuario
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            criar_usuario("admin")
        pw = re.search(r"Senha: (\S+)", buf.getvalue()).group(1)
        tok = lambda t: re.search(r'name="csrf_token" value="([^"]+)"', t).group(1)  # noqa: E731
        c.post("/login", data={"csrf_token": tok(c.get("/login").text), "username": "admin", "password": pw})

        check("cadastrados no próprio Kentro" in c.get("/modelos").text, "canal Kentro: tela explica onde cadastrar")
        with db_session() as db:
            for k, v in {"wa_provider": "meta", "wa_access_token": "t", "wa_phone_number_id": "1",
                         "wa_business_account_id": "999", "wa_simulation": "1"}.items():
                cfg.set_value(db, k, v)
            db.commit()
        page = c.get("/modelos").text
        check("hello_world" in page and "Aprovado" in page, "lista os templates da conta")

        r = c.post("/modelos/criar", data={"csrf_token": tok(page), "name": "Convite Pesquisa", "category": "MARKETING",
                                          "language": "pt_BR", "body": "Olá {{1}}, aqui é do Instituto Alves Lima. Tudo bem?",
                                          "examples": "Maria", "quick_replies": "SAIR"})
        post = [x for x in CALLS if x[0] == "POST"]
        check(post and post[-1][1] == "999/message_templates" and post[-1][2]["json"]["name"] == "convite_pesquisa",
              "envia para a conta certa com o nome ajustado (mesmo em modo simulação)")
        check("convite_pesquisa" in r.text and "Em análise" in r.text, "novo template aparece em análise")

        n = len(CALLS)
        r = c.post("/modelos/criar", data={"csrf_token": tok(r.text), "name": "ruim", "category": "MARKETING",
                                          "body": "{{1}} oi", "examples": "x"})
        check(len(CALLS) == n + 1 and "começa ou termina" in r.text and 'value="ruim"' in r.text,
              "erro de validação não chama a Meta e mantém o que foi digitado")
        r = c.post("/modelos/criar", data={"csrf_token": tok(r.text), "name": "convite_pesquisa", "category": "MARKETING",
                                          "body": "Olá, tudo bem?"})
        check("A Meta recusou" in r.text, "erro da Meta aparece na tela")

        r = c.post("/modelos/excluir", data={"csrf_token": tok(r.text), "name": "convite_pesquisa"})
        check(("DELETE", "999/message_templates", {"params": {"name": "convite_pesquisa"}}) in CALLS
              and "convite_pesquisa" not in r.text.split("Templates na Meta")[1].split("Novo template")[0], "excluir remove da Meta e da lista")
        check("cadastrar template" in c.get("/campanhas/nova").text, "atalho na tela de nova campanha")


if __name__ == "__main__":
    init_db()
    unit_checks()
    web_checks()
    print("\n" + ("TUDO OK" if not failures else f"{len(failures)} FALHA(S): {failures}"))
    sys.exit(1 if failures else 0)
