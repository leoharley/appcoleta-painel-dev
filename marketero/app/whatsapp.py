"""Cliente da API oficial do WhatsApp (WhatsApp Business Cloud API, Meta).

Regras da plataforma que o sistema respeita:
- Mensagem iniciada pela empresa (primeiro contato / disparo) só por TEMPLATE aprovado.
- Texto livre só dentro da janela de 24 h desde a última mensagem do contato.
"""
import hashlib
import hmac
import logging
import secrets

import httpx
from sqlalchemy.orm import Session

from . import settings_store as cfg

log = logging.getLogger("marketero.whatsapp")

# Erros da Cloud API com tratamento específico
ERR_NOT_ON_WHATSAPP = {131026}         # número sem WhatsApp / não entregável
ERR_OUTSIDE_WINDOW = {131047}          # mais de 24 h: exige template
ERR_USER_STOPPED_MARKETING = {131050}  # contato bloqueou mensagens de marketing


class WhatsAppError(Exception):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class WhatsAppClient:
    def __init__(self, token: str, phone_number_id: str, waba_id: str, version: str, simulation: bool):
        self.token = token
        self.phone_number_id = phone_number_id
        self.waba_id = waba_id
        self.base = f"https://graph.facebook.com/{version or 'v23.0'}"
        self.simulation = simulation

    @property
    def configured(self) -> bool:
        return bool(self.token and self.phone_number_id)

    async def _request(self, method: str, path: str, **kw) -> dict:
        if not self.configured:
            raise WhatsAppError("WhatsApp não configurado (token e Phone Number ID).")
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.request(method, f"{self.base}/{path}",
                                    headers={"Authorization": f"Bearer {self.token}"}, **kw)
        except httpx.TimeoutException as e:
            raise WhatsAppError("Tempo esgotado na API do WhatsApp; a mensagem pode ter sido enviada — "
                                "confira antes de reenviar.") from e
        except httpx.HTTPError as e:
            raise WhatsAppError(f"Sem conexão com a API do WhatsApp: {e}") from e
        try:
            data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        except ValueError:
            data = {}
        if r.status_code >= 400 or "error" in data:
            err = data.get("error", {})
            msg = err.get("error_data", {}).get("details") or err.get("message") or r.text[:300]
            raise WhatsAppError(f"{msg} (código {err.get('code')})", err.get("code"))
        return data

    async def _send(self, payload: dict) -> str:
        if self.simulation:
            return "sim-" + secrets.token_hex(8)
        payload = {"messaging_product": "whatsapp", "recipient_type": "individual", **payload}
        data = await self._request("POST", f"{self.phone_number_id}/messages", json=payload)
        try:
            return data["messages"][0]["id"]
        except (KeyError, IndexError, TypeError) as e:
            raise WhatsAppError(f"Resposta inesperada da API do WhatsApp: {str(data)[:200]}") from e

    async def send_text(self, to: str, body: str) -> str:
        return await self._send({"to": to, "type": "text", "text": {"preview_url": False, "body": body}})

    async def send_template(self, to: str, name: str, language: str, body_params: list[str]) -> str:
        template: dict = {"name": name, "language": {"code": language}}
        if body_params:
            template["components"] = [{
                "type": "body",
                "parameters": [{"type": "text", "text": p or "-"} for p in body_params],
            }]
        return await self._send({"to": to, "type": "template", "template": template})

    async def mark_read(self, message_id: str) -> None:
        if self.simulation or not self.configured:
            return
        try:
            await self._request("POST", f"{self.phone_number_id}/messages",
                                json={"messaging_product": "whatsapp", "status": "read", "message_id": message_id})
        except Exception as e:  # marcar como lida é acessório: nunca impede a resposta
            log.info("mark_read falhou: %s", e)

    async def list_templates(self) -> list[dict]:
        if not self.waba_id:
            raise WhatsAppError("Informe o WhatsApp Business Account ID nas configurações.")
        data = await self._request("GET", f"{self.waba_id}/message_templates",
                                   params={"fields": "id,name,status,language,category,components,rejected_reason",
                                           "limit": 200})
        return data.get("data", [])

    # Cadastro de templates: não envia mensagem a ninguém, por isso vale também no modo simulação.
    async def create_template(self, payload: dict) -> dict:
        if not self.waba_id:
            raise WhatsAppError("Informe o WhatsApp Business Account ID nas configurações.")
        return await self._request("POST", f"{self.waba_id}/message_templates", json=payload)

    async def delete_template(self, name: str) -> None:
        if not self.waba_id:
            raise WhatsAppError("Informe o WhatsApp Business Account ID nas configurações.")
        await self._request("DELETE", f"{self.waba_id}/message_templates", params={"name": name})


def client_from_settings(db: Session) -> WhatsAppClient:
    return WhatsAppClient(
        token=cfg.get(db, "wa_access_token"),
        phone_number_id=cfg.get(db, "wa_phone_number_id"),
        waba_id=cfg.get(db, "wa_business_account_id"),
        version=cfg.get(db, "wa_graph_version"),
        simulation=cfg.get_bool(db, "wa_simulation"),
    )


def verify_signature(app_secret: str, raw_body: bytes, header: str | None) -> bool:
    if not app_secret or not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header[7:])


def template_body_text(template: dict) -> str:
    for comp in template.get("components", []):
        if comp.get("type") == "BODY":
            return comp.get("text", "")
    return ""


def render_template_body(body: str, params: list[str]) -> str:
    for i, p in enumerate(params, start=1):
        body = body.replace("{{%d}}" % i, p)
    return body
