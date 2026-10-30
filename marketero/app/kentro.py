"""Cliente da API de integração do Kentro (atenderbem), que dá acesso ao WhatsApp da instância.

Especificação: https://<instancia>.atenderbem.com/static/getAPPAPISpecs
Todos os endpoints usados aqui são "de fila": exigem queueId + apiKey (chave da fila ou global).
"""
import logging

import httpx

log = logging.getLogger("marketero.kentro")

# Permite injetar um transporte HTTP falso nos testes.
TRANSPORT: httpx.AsyncBaseTransport | None = None

ERROR_MESSAGES = {
    "GENERIC_001": "faltam a chave da API e/ou o ID da fila",
    "AUTH_018": "chave da API inválida ou de outra fila",
    "AUTH_001": "instância bloqueada",
    "AUTH_014": "API de integração desabilitada no Kentro",
    "AUTH_021": "IP do servidor não autorizado no Kentro (libere o IP 179.197.234.17)",
    "QUEUE_008": "a fila está desabilitada",
}

# Situação de uma mensagem enfileirada (checkEnqueuedMessage.status)
ENQUEUED_STATUS = {
    0: "aguardando envio",
    1: "enviada",
    2: "número sem WhatsApp",
    3: "fila inválida ou indisponível",
    4: "número inválido",
    5: "botões inválidos",
    6: "lista inválida",
    7: "envio cancelado pela API",
    14: "falha ao enviar ao cliente",
    15: "contato bloqueado para envio ativo",
    16: "mensagem livre fora da janela de 24 h",
    98: "aguardando o fim do atendimento aberto",
}
ENQUEUED_WAITING = {0, 98}
ENQUEUED_NO_WHATSAPP = {2, 4}


class KentroError(Exception):
    def __init__(self, message: str, status: int | None = None, code: str = ""):
        super().__init__(message)
        self.status = status
        self.code = code


def phone_for_kentro(phone: str) -> tuple[str, str]:
    """(number, country): números brasileiros vão sem o 55, com country=BR, como na documentação."""
    if phone.startswith("55") and len(phone) in (12, 13):
        return phone[2:], "BR"
    return "+" + phone, ""


class KentroClient:
    def __init__(self, base_url: str, queue_id: str, api_key: str):
        self.base_url = (base_url or "").rstrip("/")
        self.queue_id = queue_id
        self.api_key = api_key

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.queue_id and self.api_key)

    async def call(self, operation: str, **payload) -> dict:
        if not self.configured:
            raise KentroError("Kentro não configurado (URL, ID da fila e chave da API).")
        try:
            queue_id = int(self.queue_id)
        except ValueError as e:
            raise KentroError("O ID da fila do Kentro deve ser um número.") from e
        body = {"queueId": queue_id, "apiKey": self.api_key, **payload}
        try:
            async with httpx.AsyncClient(timeout=20, transport=TRANSPORT) as c:
                r = await c.post(f"{self.base_url}/int/{operation}", json=body)
        except httpx.TimeoutException as e:
            raise KentroError(f"{operation}: tempo esgotado no Kentro; a operação pode ter sido feita — "
                              "confira antes de repetir.") from e
        except httpx.HTTPError as e:
            raise KentroError(f"Sem conexão com o Kentro: {e}") from e
        try:
            data = r.json()
        except ValueError:
            data = {}
        if r.status_code >= 400:
            code = str(data.get("errorCode") or "")
            msg = data.get("message") or ERROR_MESSAGES.get(code) or r.text[:200] or f"HTTP {r.status_code}"
            if code and code in ERROR_MESSAGES and ERROR_MESSAGES[code] not in msg:
                msg = f"{msg} ({ERROR_MESSAGES[code]})"
            raise KentroError(f"{operation}: {msg}" + (f" [{code}]" if code else ""), r.status_code, code)
        return data if isinstance(data, dict) else {"data": data}

    # --- Fila ---
    async def queue_status(self) -> dict:
        return await self.call("getQueueStatus")

    # --- Templates ---
    async def templates(self) -> list[dict]:
        out, page = [], 1
        while True:
            data = await self.call("getTemplates", page=page, limit=500)
            items = data.get("templates") or []
            out.extend(items)
            if len(out) >= int(data.get("total") or 0) or not items:
                return out
            page += 1

    # --- Envio ativo (campanhas) ---
    async def enqueue_template(self, phone: str, template_id: int, params: list[str], campaign: str,
                               ext_data: str, action_when_open: int) -> int:
        number, country = phone_for_kentro(phone)
        payload = {"number": number, "templateId": template_id, "varsdata": params, "campaignName": campaign[:100],
                   "extData": ext_data, "actionwhenopen": action_when_open}
        if country:
            payload["country"] = country
        data = await self.call("enqueueMessageToSend", **payload)
        if not data.get("enqueuedId"):
            raise KentroError(f"enqueueMessageToSend não devolveu o ID do envio: {data}")
        return int(data["enqueuedId"])

    async def check_enqueued(self, enqueued_id: int) -> dict:
        return await self.call("checkEnqueuedMessage", enqueuedId=enqueued_id)

    # --- Atendimentos ---
    async def open_chats(self) -> list[dict]:
        return (await self.call("getAllOpenChats")).get("chats") or []

    async def chat_messages(self, chat_id: int, from_id: int = 0) -> tuple[list[dict], int]:
        payload = {"chatId": chat_id}
        if from_id:
            payload["fromId"] = from_id
        data = await self.call("getChatMessages", **payload)
        return data.get("messages") or [], int(data.get("maxKId") or 0)

    async def send_to_chat(self, chat_id: int, text: str, info: bool = False) -> int:
        data = await self.call("sendMessageToChat", chatId=chat_id, text=text, info=info)
        return int(data.get("kId") or 0)


def template_for_ui(t: dict) -> dict:
    """Converte um template do Kentro para o formato da tela de campanhas."""
    return {
        "id": str(t.get("id")),
        "name": t.get("name") or t.get("templateName") or f"template {t.get('id')}",
        "language": t.get("language") or "pt_BR",
        "category": (t.get("category") or t.get("type") or "").lower(),
        "status": t.get("status") or "",
        "body": t.get("text") or "",
        "params": int(t.get("totalParams") or 0),
        "header": t.get("headerType") or "none",
    }


def to_seconds(ts) -> float:
    """Os timestamps do Kentro podem vir em segundos ou milissegundos."""
    try:
        v = float(ts or 0)
    except (TypeError, ValueError):
        return 0.0
    return v / 1000 if v > 1e11 else v
