"""Confere o que os SDKs da Anthropic e da OpenAI enviam de fato (corpo e cabeçalhos HTTP),
usando um servidor local que imita as respostas oficiais. Nenhuma chamada real é feita.

  MARKETERO_DATA_DIR=$(mktemp -d) MARKETERO_SECRET_KEY=t python -m tests.ai_wire_test
"""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

CAPTURED = []
MODE = {"anthropic": "ok"}

ANTHROPIC_OK = {
    "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5",
    "content": [{"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": "Olá! Fazemos pesquisas eleitorais."}],
    "stop_reason": "end_turn", "stop_sequence": None,
    "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
}
ANTHROPIC_REFUSAL = {**ANTHROPIC_OK, "content": [], "stop_reason": "refusal",
                     "stop_details": {"type": "refusal", "category": None, "explanation": "x"}}
OPENAI_OK = {
    "id": "chatcmpl-1", "object": "chat.completion", "created": 1, "model": "gpt-4.1-mini",
    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Oi do ChatGPT"}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        CAPTURED.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        if "/messages" in self.path and "chat" not in self.path:
            if MODE["anthropic"] == "401":
                return self._send(401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
            if MODE["anthropic"] == "max_tokens":
                return self._send(200, {**ANTHROPIC_OK, "stop_reason": "max_tokens"})
            return self._send(200, ANTHROPIC_REFUSAL if MODE["anthropic"] == "refusal" else ANTHROPIC_OK)
        return self._send(200, OPENAI_OK)

    def _send(self, code, data):
        raw = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


srv = HTTPServer(("127.0.0.1", 0), Handler)
port = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()
os.environ["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{port}"

from app import ai  # noqa: E402  (depois de ANTHROPIC_BASE_URL)

failures = []


def check(cond, label):
    print(("OK   " if cond else "FALHA"), label)
    if not cond:
        failures.append(label)


async def main():
    hist = [{"role": "assistant", "content": "Olá Maria (template)"},
            {"role": "user", "content": "Oi"}, {"role": "user", "content": "Que serviços vocês têm?"}]
    conf = ai.AIConfig("anthropic", "sk-ant-teste", "claude-opus-5-5", "low", "", "", "")
    reply = await ai.generate_reply(conf, "INSTRUCOES FIXAS", hist, "Dados do contato: Maria")
    req = CAPTURED[-1]
    b, h = req["body"], req["headers"]
    check(reply == "Olá! Fazemos pesquisas eleitorais.", f"Claude: resposta lida, ignorando o bloco de raciocínio ({reply!r})")
    check(req["path"].startswith("/v1/messages"), f"Claude: endpoint {req['path']}")
    # O SDK 2.x envia a chave como "Authorization: Bearer"; versões anteriores usavam "x-api-key".
    check(h.get("x-api-key") == "sk-ant-teste" or h.get("authorization") == "Bearer sk-ant-teste",
          "Claude: chave enviada no cabeçalho de autenticação")
    check("server-side-fallback-2026-07-01" in h.get("anthropic-beta", ""), f"Claude: beta do fallback ({h.get('anthropic-beta')})")
    check(b.get("fallbacks") == "default", "Claude: fallbacks=default no corpo")
    check(b.get("output_config") == {"effort": "low"}, f"Claude: output_config ({b.get('output_config')})")
    check(b.get("system") == [{"type": "text", "text": "INSTRUCOES FIXAS", "cache_control": {"type": "ephemeral"}},
                              {"type": "text", "text": "Dados do contato: Maria"}], "Claude: system em 2 blocos, cache na parte fixa")
    check(b.get("messages") == [{"role": "user", "content": "Oi\n\nQue serviços vocês têm?"}],
          f"Claude: histórico começa pelo usuário e junta mensagens seguidas ({b.get('messages')})")
    check(not any(k in b for k in ("temperature", "top_p", "top_k", "thinking")), "Claude: sem temperature/top_p/thinking")
    check(b.get("model") == "claude-opus-5-5" and b.get("max_tokens") == 8000, "Claude: modelo e max_tokens")

    conf_h = ai.AIConfig("anthropic", "k", "claude-haiku-5-5", "low", "", "", "")
    await ai.generate_reply(conf_h, "S", [{"role": "user", "content": "oi"}])
    b2, h2 = CAPTURED[-1]["body"], CAPTURED[-1]["headers"]
    check("fallbacks" not in b2 and "anthropic-beta" not in h2, "Claude Haiku: sem fallback (não suportado nele)")
    check(b2.get("output_config") == {"effort": "low"}, "Claude Haiku 5.5: com effort")
    conf_old = ai.AIConfig("anthropic", "k", "claude-haiku-4-5", "low", "", "", "")
    await ai.generate_reply(conf_old, "S", [{"role": "user", "content": "oi"}])
    check("output_config" not in CAPTURED[-1]["body"], "Claude Haiku 4.5: sem effort (não suportado nele)")

    MODE["anthropic"] = "refusal"
    try:
        await ai.generate_reply(conf, "S", [{"role": "user", "content": "oi"}])
        check(False, "Claude: recusa vira erro tratado")
    except ai.AIError as e:
        check("recusou" in str(e), f"Claude: recusa vira erro tratado ({e})")
    MODE["anthropic"] = "max_tokens"
    try:
        await ai.generate_reply(conf, "S", [{"role": "user", "content": "oi"}])
        check(False, "Claude: resposta cortada não é enviada")
    except ai.AIError as e:
        check("cortada" in str(e), f"Claude: resposta cortada não é enviada ({e})")
    MODE["anthropic"] = "401"
    try:
        await ai.generate_reply(conf, "S", [{"role": "user", "content": "oi"}])
        check(False, "Claude: chave inválida vira erro tratado")
    except ai.AIError as e:
        check("inválida" in str(e), f"Claude: chave inválida vira erro tratado ({e})")

    conf_o = ai.AIConfig("openai", "", "", "", "sk-openai-teste", "gpt-4.1-mini", f"http://127.0.0.1:{port}/v1")
    reply = await ai.generate_reply(conf_o, "FIXO", hist, "DINAMICO")
    req = CAPTURED[-1]
    check(reply == "Oi do ChatGPT", "OpenAI: resposta lida")
    check(req["path"] == "/v1/chat/completions" and req["headers"].get("authorization") == "Bearer sk-openai-teste",
          "OpenAI: endpoint e chave")
    check(req["body"]["messages"][0] == {"role": "system", "content": "FIXO\n\nDINAMICO"}
          and req["body"]["messages"][1:] == [{"role": "user", "content": "Oi\n\nQue serviços vocês têm?"}],
          "OpenAI: system + histórico")

    print("\n" + ("TUDO OK" if not failures else f"{len(failures)} FALHA(S): {failures}"))
    sys.exit(1 if failures else 0)


asyncio.run(main())
