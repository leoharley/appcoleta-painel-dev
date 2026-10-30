"""Servidor falso do Kentro (API /int/*) e da OpenAI (/v1/chat/completions) para testes de ponta a ponta.

  python tests/fake_kentro_server.py 9100
Controle do teste:
  POST /_test/inbound  {"chatId": 1, "number": "5538...", "name": "...", "text": "...", "userId": 0}
  GET  /_test/state    -> envios, mensagens enviadas aos atendimentos, enfileirados
"""
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock

LOCK = Lock()
STATE = {"chats": {}, "messages": {}, "enqueued": {}, "sent": [], "next_kid": 1000, "ai_calls": 0}


def add_msg(chat_id, direction, text, user_id=0):
    STATE["next_kid"] += 1
    STATE["messages"].setdefault(chat_id, []).append({
        "kId": STATE["next_kid"], "direction": direction, "text": text, "userId": user_id,
        "srvRcvTime": int(time.time() * 1000)})
    return STATE["next_kid"]


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code, data):
        raw = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path == "/_test/state":
            with LOCK:
                return self._json(200, {k: v for k, v in STATE.items() if k != "messages"} |
                                  {"messages": {str(k): v for k, v in STATE["messages"].items()}})
        self._json(404, {})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))) or b"{}")
        with LOCK:
            return self._route(body)

    def _route(self, body):
        if self.path == "/_test/inbound":
            cid = int(body["chatId"])
            STATE["chats"][cid] = {"chatId": cid, "clientNumber": body["number"], "clientName": body.get("name", ""),
                                   "userId": body.get("userId", 0), "onIvr": False, "status": 1}
            add_msg(cid, "in", body["text"])
            return self._json(200, {"ok": True})
        if self.path == "/_test/close":
            STATE["chats"].pop(int(body["chatId"]), None)
            return self._json(200, {"ok": True})
        if self.path.startswith("/v1/chat/completions"):
            STATE["ai_calls"] += 1
            last = body["messages"][-1]["content"]
            text = "Vou passar para a equipe comercial. [[HUMANO]]" if "proposta" in last.lower() else f"[IA de teste] Recebi: {last}"
            return self._json(200, {"id": "c", "object": "chat.completion", "created": 1, "model": body["model"],
                                    "choices": [{"index": 0, "finish_reason": "stop",
                                                 "message": {"role": "assistant", "content": text}}]})
        op = self.path.rsplit("/", 1)[-1]
        if body.get("apiKey") != "chave" or body.get("queueId") != 7:
            return self._json(401, {"errorCode": "AUTH_018"})
        if op == "getQueueStatus":
            return self._json(200, {"name": "WhatsApp Oficial (teste)", "type": "WAO", "connected": True,
                                    "authenticated": True, "authenticatedNumber": "553899990000",
                                    "openChats": len(STATE["chats"]), "loggedAgentsCount": 1})
        if op == "getTemplates":
            return self._json(200, {"total": 1, "page": 1, "limit": 500, "templates": [
                {"id": 42, "name": "apresentacao_instituto", "type": "meta", "status": "APPROVED", "language": "pt_BR",
                 "category": "MARKETING", "text": "Olá {{1}}! Aqui é do Instituto Alves Lima. Podemos conversar sobre pesquisas para o {{2}}?",
                 "totalParams": 2, "headerType": "none"}]})
        if op == "enqueueMessageToSend":
            eid = len(STATE["enqueued"]) + 1
            STATE["enqueued"][eid] = body | {"at": time.time()}
            return self._json(200, {"enqueuedId": eid})
        if op == "checkEnqueuedMessage":
            e = STATE["enqueued"].get(int(body["enqueuedId"]))
            if not e:
                return self._json(404, {"message": "não encontrada"})
            age = time.time() - e["at"]
            return self._json(200, {"status": 1 if age > 1 else 0, "kId": 1,
                                    "clientRcvTime": int(time.time() * 1000) if age > 1 else 0,
                                    "clientReadTime": int(time.time() * 1000) if age > 20 else 0})
        if op == "getAllOpenChats":
            return self._json(200, {"openChats": len(STATE["chats"]), "chats": list(STATE["chats"].values())})
        if op == "getChatMessages":
            msgs = [m for m in STATE["messages"].get(int(body["chatId"]), []) if m["kId"] >= body.get("fromId", 0)]
            return self._json(200, {"messages": msgs, "maxKId": max([m["kId"] for m in msgs], default=0)})
        if op == "sendMessageToChat":
            STATE["sent"].append({"chatId": body["chatId"], "text": body.get("text"), "info": body.get("info", False)})
            kid = add_msg(int(body["chatId"]), "info" if body.get("info") else "out", body.get("text"))
            return self._json(200, {"kId": kid, "mId": f"wamid.{kid}"})
        return self._json(404, {"message": f"{op} não simulado"})


if __name__ == "__main__":
    ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
