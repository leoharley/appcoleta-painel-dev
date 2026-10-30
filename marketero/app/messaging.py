"""Canal de envio do WhatsApp: Kentro (atenderbem) ou Cloud API da Meta direto.

O resto do sistema usa só esta interface:
  channel = get_channel(db)
  await channel.send_text(conv, texto)                       -> id externo da mensagem
  await channel.send_template(contato, campanha, params, ref) -> (id externo, status do destinatário)
  await channel.list_templates()                              -> templates no formato da tela
"""
import secrets

from sqlalchemy.orm import Session

from . import settings_store as cfg
from .kentro import KentroClient, KentroError, template_for_ui
from .models import Campaign, Contact, Conversation
from .whatsapp import (ERR_NOT_ON_WHATSAPP, ERR_USER_STOPPED_MARKETING,
                       WhatsAppError, client_from_settings,
                       template_body_text)


class MessagingError(Exception):
    def __init__(self, message: str, no_whatsapp: bool = False, opted_out: bool = False):
        super().__init__(message)
        self.no_whatsapp = no_whatsapp
        self.opted_out = opted_out


class MetaChannel:
    key, label = "meta", "Cloud API da Meta"

    def __init__(self, db: Session):
        self.wa = client_from_settings(db)
        self.simulation = self.wa.simulation

    @property
    def configured(self) -> bool:
        return self.wa.configured

    async def send_text(self, conv: Conversation, text: str) -> str:
        try:
            return await self.wa.send_text(conv.contact.phone, text)
        except WhatsAppError as e:
            raise MessagingError(str(e)) from e

    async def send_template(self, contact: Contact, campaign: Campaign, params: list[str], ref: str) -> tuple[str, str]:
        try:
            wamid = await self.wa.send_template(contact.phone, campaign.template_ref or campaign.template_name,
                                                campaign.template_language, params)
        except WhatsAppError as e:
            raise MessagingError(str(e), no_whatsapp=e.code in ERR_NOT_ON_WHATSAPP,
                                 opted_out=e.code in ERR_USER_STOPPED_MARKETING) from e
        return wamid, "sent"

    async def list_templates(self) -> list[dict]:
        try:
            items = await self.wa.list_templates()
        except WhatsAppError as e:
            raise MessagingError(str(e)) from e
        return [{"id": t["name"], "name": t["name"], "language": t.get("language", "pt_BR"),
                 "category": (t.get("category") or "").lower(), "status": t.get("status", ""),
                 "body": template_body_text(t), "params": template_body_text(t).count("{{"), "header": ""}
                for t in items if t.get("status") == "APPROVED"]

    async def mark_read(self, message_id: str) -> None:
        await self.wa.mark_read(message_id)

    async def test(self) -> str:
        names = [t["name"] for t in await self.list_templates()]
        return f"Conexão com a Cloud API OK. Templates aprovados: {', '.join(names) or 'nenhum'}."


class KentroChannel:
    key, label = "kentro", "Kentro (atenderbem)"

    def __init__(self, db: Session):
        self.client = KentroClient(cfg.get(db, "kentro_base_url"), cfg.get(db, "kentro_queue_id"),
                                   cfg.get(db, "kentro_api_key"))
        self.simulation = cfg.get_bool(db, "wa_simulation")
        self.action_when_open = cfg.get_int(db, "kentro_action_when_open", 1)

    @property
    def configured(self) -> bool:
        return self.client.configured

    async def send_text(self, conv: Conversation, text: str) -> str:
        if not conv.external_chat_id:
            raise MessagingError("O contato não tem atendimento aberto no Kentro: a conversa só pode ser "
                                 "retomada com um template (crie uma campanha só com este contato).")
        if self.simulation:
            return "sim-" + secrets.token_hex(8)
        try:
            return f"k{await self.client.send_to_chat(conv.external_chat_id, text)}"
        except KentroError as e:
            raise MessagingError(str(e)) from e

    async def send_note(self, conv: Conversation, text: str) -> None:
        """Nota interna no atendimento: aparece para os atendentes, não vai para o cliente."""
        if self.simulation or not conv.external_chat_id:
            return
        try:
            await self.client.send_to_chat(conv.external_chat_id, text, info=True)
        except KentroError:
            pass

    async def send_template(self, contact: Contact, campaign: Campaign, params: list[str], ref: str) -> tuple[str, str]:
        if self.simulation:
            return "sim-" + secrets.token_hex(8), "sent"
        try:
            template_id = int(campaign.template_ref)
        except (TypeError, ValueError) as e:
            raise MessagingError("Template sem ID do Kentro: recrie a campanha escolhendo o template da lista.") from e
        try:
            enqueued = await self.client.enqueue_template(contact.phone, template_id, params, campaign.name, ref,
                                                          self.action_when_open)
        except KentroError as e:
            raise MessagingError(str(e)) from e
        return f"q{enqueued}", "queued"

    async def list_templates(self) -> list[dict]:
        try:
            items = await self.client.templates()
        except KentroError as e:
            raise MessagingError(str(e)) from e
        out = []
        for t in items:
            if t.get("type") == "meta" and t.get("status") not in ("APPROVED", "", None):
                continue  # pendentes/rejeitados na Meta não podem ser enviados
            out.append(template_for_ui(t))
        return out

    async def mark_read(self, message_id: str) -> None:
        return None

    async def test(self) -> str:
        try:
            q = await self.client.queue_status()
        except KentroError as e:
            raise MessagingError(str(e)) from e
        templates = await self.list_templates()
        situation = "conectada e autenticada" if q.get("connected") and q.get("authenticated") else "SEM CONEXÃO com o WhatsApp"
        return (f"Kentro OK. Fila \"{q.get('name')}\" ({q.get('type')}, {situation}, número "
                f"{q.get('authenticatedNumber') or '-'}); {q.get('openChats', 0)} atendimento(s) aberto(s), "
                f"{q.get('loggedAgentsCount', 0)} atendente(s) logado(s); {len(templates)} template(s) disponível(is).")


def get_channel(db: Session) -> MetaChannel | KentroChannel:
    return KentroChannel(db) if (cfg.get(db, "wa_provider") or "kentro") == "kentro" else MetaChannel(db)
